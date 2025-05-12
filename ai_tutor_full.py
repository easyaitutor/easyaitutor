import os
import io
import json
import traceback
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone as dt_timezone
import uuid
import random
import time # Added for the final __main__ block sleep

import openai
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import jwt
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# --- Configuration ---
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-super-secret-key-in-production")
if JWT_SECRET_KEY == "change-this-super-secret-key-in-production":
    print("WARNING: JWT_SECRET_KEY is set to its default insecure value. Please set a strong secret key.")
LINK_VALIDITY_HOURS = 6

EASYAI_TUTOR_PROGRESS_API_ENDPOINT = os.getenv("EASYAI_TUTOR_PROGRESS_API_ENDPOINT")

days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
            "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

# --- PDF loader & Section Splitter ---
try:
    import fitz  # PyMuPDF
    def split_sections(pdf_file):
        doc = fitz.open(pdf_file.name) if hasattr(pdf_file, "name") else fitz.open(
            stream=pdf_file.read(), filetype="pdf"
        )
        pages_text = [page.get_text() for page in doc]
        doc.close()
        headings = []
        for i, text in enumerate(pages_text):
            for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad)\s+[\d\w]+.*", text):
                headings.append({"page": i + 1, "start_char_index": m.start(), "title": m.group().strip(), "page_index": i})
        
        headings.sort(key=lambda h: (h['page_index'], h['start_char_index']))
        
        sections = []
        if not headings:
            full_content = "\n".join(pages_text)
            if full_content.strip():
                 sections.append({'title': 'Full Document Content', 'content': full_content.strip(), 'page': 1})
            return sections

        for idx, h in enumerate(headings):
            current_page_idx = h['page_index']
            start_char_on_page = h['start_char_index']
            content = ''
            if idx + 1 < len(headings):
                next_h = headings[idx+1]
                next_page_idx = next_h['page_index']
                end_char_on_page = next_h['start_char_index']
                if current_page_idx == next_page_idx:
                    content += pages_text[current_page_idx][start_char_on_page:end_char_on_page]
                else:
                    content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                    for p_idx in range(current_page_idx + 1, next_page_idx):
                        content += pages_text[p_idx] + '\n'
                    content += pages_text[next_page_idx][:end_char_on_page]
            else:
                content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                for p_idx in range(current_page_idx + 1, len(pages_text)):
                    content += pages_text[p_idx] + '\n'
            if content.strip():
                sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
        # Ensure sections are only added if they have meaningful content beyond just the title
        sections = [s for s in sections if len(s['content']) > len(s['title']) + 5] # Heuristic: content should be longer than title
        return sections

except ImportError:
    print("PyMuPDF (fitz) not found, falling back to PyPDF2. Section splitting will be basic.")
    from PyPDF2 import PdfReader
    def split_sections(pdf_file):
        if hasattr(pdf_file, "name"):
            reader = PdfReader(pdf_file.name)
        elif isinstance(pdf_file, io.BytesIO):
             pdf_file.seek(0)
             reader = PdfReader(pdf_file)
        else:
            reader = PdfReader(str(pdf_file))
        text = "\n".join(page.extract_text() or '' for page in reader.pages)
        chunks = re.split(r'(?<=[.?!])\s+', text)
        sections = []
        chunk_size = 10 
        for i in range(0, len(chunks), chunk_size):
            title = f"Lesson Part {i//chunk_size+1}"
            content = " ".join(chunks[i:i+chunk_size]).strip()
            if content:
                sections.append({'title': title, 'content': content, 'page': None})
        if not sections and text.strip():
            sections.append({'title': 'Full Document Content', 'content': text.strip(), 'page': None})
        return sections

# --- Helpers ---
def download_docx(content, filename):
    buf = io.BytesIO()
    doc = Document()
    # Basic handling for bold text marked with **text**
    for line in content.split("\n"):
        paragraph = doc.add_paragraph()
        parts = re.split(r'(\*\*.*?\*\*)', line) # Split by bold markers
        for part in parts:
            if part.startswith('**') and part.endswith('**'):
                paragraph.add_run(part[2:-2]).bold = True
            else:
                paragraph.add_run(part)
    doc.save(buf)
    buf.seek(0)
    return buf, filename

def count_classes(sd, ed, wdays):
    cnt, cur = 0, sd
    while cur <= ed:
        if cur.weekday() in wdays:
            cnt += 1
        cur += timedelta(days=1)
    return cnt

def generate_access_token(student_id, course_id, lesson_id_or_num, lesson_date_obj):
    if isinstance(lesson_date_obj, str):
        lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()
    issue_datetime_utc = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    expiration_datetime_utc = issue_datetime_utc + timedelta(hours=LINK_VALIDITY_HOURS)
    payload = {
        "sub": student_id, "course_id": course_id, "lesson_id": lesson_id_or_num,
        "iat": issue_datetime_utc, "exp": expiration_datetime_utc, "aud": "https://www.easyaitutor.com"
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def generate_5_digit_code():
    return str(random.randint(10000, 99999))

def send_email_notification(to_email, subject, html_content, student_name="User"):
    if not SMTP_USER or not SMTP_PASS:
        print(f"SMTP credentials not set. Cannot send email to {to_email}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.add_alternative(html_content, subtype='html')
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print(f"Email sent to {to_email} with subject: {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email to {to_email}: {e}\n{traceback.format_exc()}")
        return False

# --- Generate Syllabus ---
def generate_syllabus(cfg):
    sd = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date()
    ed = datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    mr = f"{sd.strftime('%B')}–{ed.strftime('%B')}"
    total = count_classes(sd, ed, [days_map[d] for d in cfg['class_days']])
    header = [
        f"Course Name: {cfg['course_name']}",
        f"Professor:   {cfg['instructor']['name']}",
        f"Email:       {cfg['instructor']['email']}",
        f"Duration:    {mr} ({total} classes)",
        '_'*60
    ]
    objectives = [f" • {o}" for o in cfg['learning_objectives']]
    body = [
        "COURSE DESCRIPTION:", cfg['course_description'], "", "OBJECTIVES:"
    ] + objectives + [
        "", "GRADING & ASSESSMENTS:", " • Each class includes a quiz.",
        " • If score < 60%, student may retake the quiz next day.",
        " • Final grade = average of all quiz scores.", "", "SCHEDULE OVERVIEW:",
        f" • {mr}, every {', '.join(cfg['class_days'])}", "", "OFFICE HOURS & SUPPORT:",
        " • Office Hours: Tuesdays 3–5 PM; Thursdays 10–11 AM (Zoom)",
        " • Email response within 24 hours on weekdays"
    ]
    return "\n".join(header + [""] + body)

# --- Generate Lesson Plan by Week (Structured and Formatted) ---
def generate_plan_by_week_structured_and_formatted(cfg):
    sd = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date()
    ed = datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    wdays = {days_map[d] for d in cfg['class_days']}
    
    class_dates = []
    cur = sd
    while cur <= ed:
        if cur.weekday() in wdays:
            class_dates.append(cur)
        cur += timedelta(days=1)

    summaries = []
    course_sections = cfg.get('sections', [])
    if not isinstance(course_sections, list): course_sections = []

    if not course_sections:
        print("Warning: No sections found in config to generate lesson plan summaries.")
        for _ in class_dates:
             summaries.append("Topic to be determined (No source material sections found).")
    else:
        for sec_idx in range(len(class_dates)): # Iterate based on number of class dates
            if sec_idx < len(course_sections): # If there's a corresponding section
                sec = course_sections[sec_idx]
                try:
                    section_content = sec.get('content', '')
                    if not isinstance(section_content, str): section_content = str(section_content)
                    if not section_content.strip(): # Skip if section content is empty
                         summaries.append(f"Review or Practice ({sec.get('title', 'Previous Topic')})")
                         continue

                    resp = openai.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role":"system","content":"Summarize this section's main topic in one clear sentence suitable for a lesson plan. Be specific."},
                            {"role":"user","content": section_content[:4000]}
                        ], temperature=0.6, max_tokens=100
                    )
                    summaries.append(resp.choices[0].message.content.strip())
                except Exception as e:
                    print(f"Error generating summary for section {sec_idx} ('{sec.get('title', 'Unknown Title')}'): {e}")
                    # Fallback: Use the original section title if summary fails
                    summaries.append(f"{sec.get('title', 'Original Topic Summary Error')}")
            else: # More class dates than sections
                summaries.append("Topic to be announced or class review.")
    
    lessons_by_week = {}
    structured_lessons = []

    for idx, dt_obj in enumerate(class_dates):
        week_number = dt_obj.isocalendar()[1]
        year_of_week = dt_obj.isocalendar()[0] 
        week_key = f"{year_of_week}-W{week_number:02d}"
        summary_for_lesson = summaries[idx] if idx < len(summaries) else "Topic to be announced."
        page_num, original_title = None, "N/A"
        if idx < len(course_sections):
            page_num = course_sections[idx].get('page')
            original_title = course_sections[idx].get('title', 'N/A')
        lesson_data = {
            "lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'),
            "topic_summary": summary_for_lesson, "original_section_title": original_title,
            "page_reference": page_num
        }
        structured_lessons.append(lesson_data)
        lessons_by_week.setdefault(week_key, []).append(lesson_data)

    formatted_lines = []
    for week_key in sorted(lessons_by_week.keys()):
        year_disp, week_num_disp = week_key.split("-W")
        # Bold Week heading
        formatted_lines.append(f"**Week {week_num_disp} (Year {year_disp})**\n")
        for lesson in lessons_by_week[week_key]:
            ds = datetime.strptime(lesson['date'], '%Y-%m-%d').strftime('%B %d, %Y')
            pstr = f" (Ref. p. {lesson['page_reference']})" if lesson['page_reference'] else ''
            # Bold Lesson number and Date
            formatted_lines.append(f"**Lesson {lesson['lesson_number']} ({ds})**{pstr}: {lesson['topic_summary']}")
        formatted_lines.append('') # Keep blank line for spacing
    
    return "\n".join(formatted_lines), structured_lessons

# --- APScheduler Setup ---
scheduler = BackgroundScheduler(timezone="UTC")

# --- Scheduler Jobs ---
def send_daily_class_reminders():
    print(f"SCHEDULER: Running daily class reminder job at {datetime.now(dt_timezone.utc)}")
    today_utc = datetime.now(dt_timezone.utc).date()
    for config_file in CONFIG_DIR.glob("*_config.json"):
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            course_id, course_name = config_file.stem.replace("_config", ""), cfg.get("course_name", "N/A")
            if not cfg.get("lessons") or not cfg.get("students"): continue
            for lesson in cfg["lessons"]:
                lesson_date = datetime.strptime(lesson["date"], '%Y-%m-%d').date()
                if lesson_date == today_utc:
                    print(f"SCHEDULER: Class found for {course_name} today: Lesson {lesson['lesson_number']}")
                    class_code = generate_5_digit_code()
                    for student in cfg["students"]:
                        student_id, student_email, student_name = student.get("id", "unknown"), student.get("email"), student.get("name", "Student")
                        if not student_email: continue
                        token = generate_access_token(student_id, course_id, lesson["lesson_number"], lesson_date)
                        access_link = f"https://www.easyaitutor.com/class?token={token}"
                        email_subject = f"Today's Class Link for {course_name}: {lesson['topic_summary']}"
                        email_html_body = f"""
                        <html><head><style>body {{font-family: sans-serif;}} strong {{color: #007bff;}} a {{color: #0056b3;}} .container {{padding: 20px; border: 1px solid #ddd; border-radius: 5px;}} .code {{font-size: 1.5em; font-weight: bold; background-color: #f0f0f0; padding: 5px 10px;}}</style></head>
                        <body><div class="container">
                            <p>Hi {student_name},</p>
                            <p>Your class for <strong>{course_name}</strong> - "{lesson['topic_summary']}" - is today!</p>
                            <p>Access link: <a href="{access_link}">{access_link}</a></p>
                            <p>5-digit code: <span class="code">{class_code}</span></p>
                            <p>Valid from <strong>6:00 AM to 12:00 PM UTC</strong> today ({today_utc.strftime('%B %d, %Y')}).</p>
                            <p>Best regards,<br>AI Tutor System</p>
                        </div></body></html>"""
                        send_email_notification(student_email, email_subject, email_html_body, student_name)
        except Exception as e: print(f"SCHEDULER: Error in daily reminders for {config_file.name}: {e}\n{traceback.format_exc()}")

def check_student_progress_and_notify_professor():
    print(f"SCHEDULER: Running student progress check at {datetime.now(dt_timezone.utc)}")
    if not EASYAI_TUTOR_PROGRESS_API_ENDPOINT:
        print("SCHEDULER: EASYAI_TUTOR_PROGRESS_API_ENDPOINT not set. Skipping.")
        return
    yesterday_utc = datetime.now(dt_timezone.utc).date() - timedelta(days=1)
    for config_file in CONFIG_DIR.glob("*_config.json"):
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            course_id, course_name = config_file.stem.replace("_config", ""), cfg.get("course_name", "N/A")
            instructor_cfg = cfg.get("instructor", {})
            instructor_email, instructor_name = instructor_cfg.get("email"), instructor_cfg.get("name", "Instructor")
            if not instructor_email or not cfg.get("students") or not cfg.get("lessons"): continue
            for lesson in cfg.get("lessons", []):
                lesson_date = datetime.strptime(lesson["date"], '%Y-%m-%d').date()
                if lesson_date != yesterday_utc: continue
                lesson_id_for_api = lesson["lesson_number"]
                print(f"SCHEDULER: Checking progress for {course_name}, Lesson {lesson_id_for_api}")
                for student in cfg.get("students", []):
                    student_id, student_name = student.get("id"), student.get("name", "Student")
                    if not student_id: continue
                    try:
                        response = requests.get(EASYAI_TUTOR_PROGRESS_API_ENDPOINT, params={"course_id": course_id, "student_id": student_id, "lesson_id": lesson_id_for_api}, timeout=10)
                        response.raise_for_status()
                        progress_data = response.json()
                        quiz_score, engagement = progress_data.get("quiz_score"), progress_data.get("engagement_level")
                        needs_attention, reasons = False, []
                        if quiz_score is not None and isinstance(quiz_score, (int, float)) and quiz_score < 60:
                            needs_attention, reasons = True, reasons + [f"Quiz score {quiz_score}% (<60%)"]
                        if isinstance(engagement, str) and engagement.lower() == "low":
                            needs_attention, reasons = True, reasons + ["Low engagement reported"]
                        if needs_attention:
                            print(f"SCHEDULER: Alert for {student_name}, {course_name}, lesson {lesson_id_for_api}.")
                            subject = f"Student Progress Alert: {student_name} in {course_name}"
                            reasons_html = "".join([f"<li>{r}</li>" for r in reasons])
                            details_url = progress_data.get('details_url')
                            details_link_html = f"<p>Details: <a href='{details_url}'>View Progress</a></p>" if details_url else ""
                            body_html = f"""<html><body><p>Dear {instructor_name},</p><p>Alert for <strong>{student_name}</strong> in <strong>{course_name}</strong> (Lesson "{lesson.get('topic_summary', lesson_id_for_api)}", Date: {lesson_date.strftime('%B %d, %Y')}):</p><ul>{reasons_html}</ul>{details_link_html}<p>Please consider engaging with the student.</p><p>AI Tutor Monitoring</p></body></html>"""
                            send_email_notification(instructor_email, subject, body_html, instructor_name)
                    except Exception as e_prog: print(f"SCHEDULER: Error processing progress for {student_name}: {e_prog}")
        except Exception as e_course: print(f"SCHEDULER: Error in progress check for {config_file.name}: {e_course}")

# --- Gradio Callbacks ---
def _get_syllabus_text_from_config(course_name_str):
    """Helper to fetch syllabus text from config."""
    if not course_name_str:
        return "Error: Course name not available to fetch syllabus."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists():
        return f"Error: Config for '{course_name_str}' not found."
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return generate_syllabus(cfg)
    except Exception as e:
        return f"Error loading syllabus from config: {e}"

def enable_edit_syllabus_and_reload(current_course_name, current_output_content):
    """Makes output box editable. If it contains status msg, reloads syllabus."""
    # Simple check: if it doesn't look like the start of a syllabus, reload.
    if not current_output_content.strip().startswith("Course Name:"):
        syllabus_text = _get_syllabus_text_from_config(current_course_name)
        # Make interactive AND update value
        return gr.update(value=syllabus_text, interactive=True)
    # Otherwise, just make it interactive
    return gr.update(interactive=True)

def enable_edit_plan():
    """Makes lesson plan output box editable."""
    return gr.update(interactive=True)

def save_setup(course_name, instr_name, instr_email, devices, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected, students_input_str):
    """Processes inputs, generates syllabus, saves config, updates UI."""
    # Expected outputs: output_box, btn_save, dummy_btn_show_syllabus, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan, syllabus_actions_row, plan_buttons_row, output_plan_box
    num_expected_outputs = 11

    def error_return_tuple(error_message_str):
        """Returns tuple for error state, matching expected outputs."""
        # Ensure output_box is visible to show the error
        return (
            gr.update(value=error_message_str, visible=True, interactive=False), # 1. output_box
            gr.update(visible=True),  # 2. btn_save (keep visible)
            None,                     # 3. dummy_btn_show_syllabus
            gr.update(visible=False), # 4. btn_generate_plan
            gr.update(visible=False), # 5. btn_edit_syl
            gr.update(visible=False), # 6. btn_email_syl
            gr.update(visible=False), # 7. btn_edit_plan
            gr.update(visible=False), # 8. btn_email_plan
            gr.update(visible=False), # 9. syllabus_actions_row
            gr.update(visible=False), # 10. plan_buttons_row
            gr.update(value="", visible=False) # 11. output_plan_box
        )

    try:
        # Basic validation
        if not all([course_name, instr_name, instr_email, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected]):
            return error_return_tuple("⚠️ Error: All fields marked with * are required.")
        
        # Validate dates
        try:
            start_dt = datetime(int(sy), int(sm), int(sd_day))
            end_dt = datetime(int(ey), int(em), int(ed_day))
            if end_dt <= start_dt:
                 return error_return_tuple("⚠️ Error: End date must be after start date.")
        except ValueError:
             return error_return_tuple("⚠️ Error: Invalid date selected.")

        sections = split_sections(pdf_file)
        if not sections:
             return error_return_tuple("⚠️ Error: Could not extract any meaningful sections from the PDF. Check PDF content/structure.")

        # Generate description and objectives using AI
        full_content_for_ai = "\n\n".join(f"Title: {s['title']}\nContent Snippet: {s['content'][:1000]}" for s in sections)
        r1 = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"Generate a concise course description (2-3 sentences)."},{"role":"user","content": full_content_for_ai}])
        desc = r1.choices[0].message.content.strip()
        r2 = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"Generate 5–10 clear, actionable learning objectives. Start each with a verb."},{"role":"user","content": full_content_for_ai}])
        objs = [ln.strip(" -•*") for ln in r2.choices[0].message.content.splitlines() if ln.strip()]

        # Parse students
        parsed_students = [{"id": str(uuid.uuid4()), "name": n.strip(), "email": e.strip()} for ln in students_input_str.splitlines() if ',' in ln for n, e in [ln.split(',', 1)]]

        # Create config dictionary
        cfg = {
            "course_name": course_name, "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days_selected, "start_date": f"{sy}-{sm}-{sd_day}", "end_date": f"{ey}-{em}-{ed_day}",
            "allowed_devices": devices, "students": parsed_students, "sections": sections,
            "course_description": desc, "learning_objectives": objs,
            "lessons": [], "lesson_plan_formatted": "" # Initialize lesson plan fields
        }

        # Save config file
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        # Generate syllabus text
        syllabus_text = generate_syllabus(cfg)

        # Return success tuple for UI update
        return (
            gr.update(value=syllabus_text, visible=True, interactive=False), # 1. output_box
            gr.update(visible=False),  # 2. btn_save (hide)
            None,                      # 3. dummy_btn_show_syllabus
            gr.update(visible=True),   # 4. btn_generate_plan (show)
            gr.update(visible=True),   # 5. btn_edit_syl (show)
            gr.update(visible=True),   # 6. btn_email_syl (show)
            gr.update(visible=False),  # 7. btn_edit_plan (hide)
            gr.update(visible=False),  # 8. btn_email_plan (hide)
            gr.update(visible=True),   # 9. syllabus_actions_row (show)
            gr.update(visible=True),   # 10. plan_buttons_row (show - contains generate btn)
            gr.update(value="", visible=False) # 11. output_plan_box (clear/hide)
        )
    except openai.APIError as oai_err:
        print(f"OpenAI Error in save_setup: {oai_err}\n{traceback.format_exc()}")
        return error_return_tuple(f"⚠️ OpenAI API Error: {oai_err}. Check API key/quota.")
    except Exception as e:
        print(f"Error in save_setup: {e}\n{traceback.format_exc()}")
        return error_return_tuple(f"⚠️ Unexpected Error during setup: {e}")

def generate_plan_callback(course_name_from_input):
    """Generates lesson plan, saves to config, updates UI."""
    # Expected outputs: output_plan_box, dummy_btn_save, dummy_btn_show_syllabus, btn_generate_plan, dummy_btn_edit_syl, dummy_btn_email_syl, btn_edit_plan, btn_email_plan
    num_expected_outputs = 8

    def error_return_for_plan(error_message_str):
        """Returns tuple for error state, matching expected outputs."""
        return (
            gr.update(value=error_message_str, visible=True, interactive=False), # 1. output_plan_box
            None, # 2. dummy_btn_save
            None, # 3. dummy_btn_show_syllabus
            gr.update(visible=True), # 4. btn_generate_plan (keep visible)
            None, # 5. dummy_btn_edit_syl
            None, # 6. dummy_btn_email_syl
            gr.update(visible=False), # 7. btn_edit_plan
            gr.update(visible=False)  # 8. btn_email_plan
        )

    try:
        if not course_name_from_input:
            return error_return_for_plan("⚠️ Error: Course Name is required to generate a lesson plan.")
        
        path = CONFIG_DIR / f"{course_name_from_input.replace(' ','_').lower()}_config.json"
        if not path.exists():
            return error_return_for_plan(f"⚠️ Error: Config for '{course_name_from_input}' not found. Please save setup first.")

        cfg = json.loads(path.read_text(encoding="utf-8"))
        
        # Generate the plan (both formatted string and structured list)
        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg)
        
        # Update config with generated plan
        cfg["lessons"] = structured_lessons_list
        cfg["lesson_plan_formatted"] = formatted_plan_str
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # Return success tuple for UI update
        return (
            gr.update(value=formatted_plan_str, visible=True, interactive=False), # 1. output_plan_box
            None, # 2. dummy_btn_save
            None, # 3. dummy_btn_show_syllabus
            gr.update(visible=True),  # 4. btn_generate_plan (remains visible)
            None, # 5. dummy_btn_edit_syl
            None, # 6. dummy_btn_email_syl
            gr.update(visible=True),   # 7. btn_edit_plan (now visible)
            gr.update(visible=True)    # 8. btn_email_plan (now visible)
        )
    except openai.APIError as oai_err:
        print(f"OpenAI Error in generate_plan_callback: {oai_err}\n{traceback.format_exc()}")
        return error_return_for_plan(f"⚠️ OpenAI API Error during plan generation: {oai_err}.")
    except Exception as e:
        print(f"Error in generate_plan_callback: {e}\n{traceback.format_exc()}")
        return error_return_for_plan(f"⚠️ Error generating lesson plan: {e}")

def email_document_callback(course_name, doc_type, output_text_content, students_input_str):
    """Emails syllabus or lesson plan as DOCX."""
    if not SMTP_USER or not SMTP_PASS:
        # Update the output box directly with the error message
        return gr.update(value="⚠️ Error: SMTP settings (User/Pass) not configured. Cannot send email.")
    try:
        if not course_name or not output_text_content:
             return gr.update(value=f"⚠️ Error: Course Name and {doc_type} content are required.")

        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists():
             return gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        
        instr_name = cfg.get("instructor", {}).get("name", "Instructor")
        instr_email = cfg.get("instructor", {}).get("email")

        # Use the potentially edited content from the output box
        buf, fn = download_docx(output_text_content, f"{course_name.replace(' ','_')}_{doc_type.lower()}.docx")
        attachment_data = buf.read()
        
        # Get recipients from UI input + instructor from config
        recipients = []
        if instr_email: recipients.append({"name": instr_name, "email": instr_email})
        for line in students_input_str.splitlines():
            if ',' in line:
                name, email_addr = line.split(',', 1)
                recipients.append({"name": name.strip(), "email": email_addr.strip()})
        
        if not recipients:
             return gr.update(value=f"⚠️ Error: No recipients found (check instructor email in config and student list input).")

        success_count = 0
        errors = []
        for rec in recipients:
            msg = EmailMessage()
            msg["Subject"] = f"Course {doc_type.capitalize()}: {course_name}"
            msg["From"] = SMTP_USER
            msg["To"] = rec["email"]
            msg.set_content(f"Hi {rec['name']},\n\nAttached is the {doc_type.lower()} for the course: {course_name}.\n\nBest regards,\nAI Tutor System")
            msg.add_attachment(attachment_data, maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=fn)
            
            try:
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                    s.starttls()
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
                success_count +=1
            except Exception as e_smtp:
                error_msg = f"SMTP Error to {rec['email']}: {e_smtp}"
                print(error_msg)
                errors.append(error_msg)
        
        status_message = f"✅ {doc_type.capitalize()} emailed to {success_count} recipient(s)."
        if errors:
            status_message += f"\n⚠️ Errors occurred:\n" + "\n".join(errors)
        # Update the output box with the status
        return gr.update(value=status_message)

    except Exception as e:
        error_text = f"⚠️ Error emailing {doc_type.lower()}:\n{traceback.format_exc()}"
        print(error_text)
        return gr.update(value=error_text) # Update output box with error

def email_syllabus_callback(course_name, students_input_str, output_box_content):
    """Callback wrapper for emailing syllabus."""
    return email_document_callback(course_name, "Syllabus", output_box_content, students_input_str)

def email_plan_callback(course_name, students_input_str, output_box_content):
    """Callback wrapper for emailing lesson plan."""
    return email_document_callback(course_name, "Lesson Plan", output_box_content, students_input_str)

# --- Build UI ---
def build_ui():
    with gr.Blocks(theme=gr.themes.Soft()) as demo:
        gr.Markdown("## AI Tutor Instructor Panel")
        with gr.Tabs():
            with gr.TabItem("Course Setup & Syllabus"):
                with gr.Row():
                    course = gr.Textbox(label="Course Name*", placeholder="e.g., Introduction to Python")
                    instr  = gr.Textbox(label="Instructor Name*", placeholder="e.g., Dr. Ada Lovelace")
                    email  = gr.Textbox(label="Instructor Email*", placeholder="e.g., ada@example.com", type="email")
                pdf_file = gr.File(label="Upload Course Material PDF*", file_types=[".pdf"])
                with gr.Row():
                    with gr.Column(scale=2): # Make schedule column wider
                        gr.Markdown("#### Course Schedule")
                        years    = [str(y) for y in range(datetime.now().year, datetime.now().year + 5)]
                        months   = [f"{m:02d}" for m in range(1,13)]
                        days_list= [f"{d:02d}" for d in range(1,32)]
                        with gr.Row(): sy, sm, sd_day = gr.Dropdown(years, label="Start Year*"), gr.Dropdown(months, label="Start Month*"), gr.Dropdown(days_list, label="Start Day*")
                        with gr.Row(): ey, em, ed_day = gr.Dropdown(years, label="End Year*"), gr.Dropdown(months, label="End Month*"), gr.Dropdown(days_list, label="End Day*")
                        class_days_selected = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*")
                    with gr.Column(scale=1): # Make student column narrower
                        gr.Markdown("#### Student & Access")
                        devices = gr.CheckboxGroup(["Phone","PC", "Tablet"], label="Allowed Devices", value=["PC"])
                        students_input_str = gr.Textbox(label="Students (Name,Email per line)", lines=5, placeholder="S. One,s1@ex.com\nS. Two,s2@ex.com")
                
                btn_save = gr.Button("1. Save Setup & Generate Syllabus", variant="primary")
                gr.Markdown("---")
                # Output box: initial visible=False, label changed
                output_box = gr.Textbox(label="Output", lines=20, interactive=False, visible=False, show_copy_button=True) 
                # Syllabus actions row: initial visible=False
                with gr.Row(visible=False) as syllabus_actions_row:
                    btn_edit_syl  = gr.Button(value="📝 Edit Syllabus Text") 
                    btn_email_syl = gr.Button(value="📧 Email Syllabus", variant="secondary")

            with gr.TabItem("Lesson Plan Management"):
                gr.Markdown("Enter course name (auto-filled from Tab 1 if set there), then generate plan.")
                course_load_for_plan = gr.Textbox(label="Course Name for Lesson Plan", placeholder="e.g., Introduction to Python")
                output_plan_box = gr.Textbox(label="Lesson Plan Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                # Plan buttons row: initial visible=False
                with gr.Row(visible=False) as plan_buttons_row:
                    btn_generate_plan = gr.Button("2. Generate/Re-generate Lesson Plan", variant="primary")
                    btn_edit_plan = gr.Button(value="📝 Edit Plan Text")
                    btn_email_plan= gr.Button(value="📧 Email Lesson Plan", variant="secondary")
        
        # --- Event Handlers ---
        # Define dummy components to match the expected number of outputs if a callback doesn't affect them
        dummy_btn_1 = gr.Button(visible=False) # Placeholder for btn_show_syllabus_hidden
        dummy_btn_2 = gr.Button(visible=False) # Placeholder for btn_save in plan callback
        dummy_btn_3 = gr.Button(visible=False) # Placeholder for btn_edit_syl in plan callback
        dummy_btn_4 = gr.Button(visible=False) # Placeholder for btn_email_syl in plan callback

        # Save Setup Button Click
        btn_save.click(
            save_setup, 
            inputs=[course,instr,email,devices,pdf_file,sy,sm,sd_day,ey,em,ed_day,class_days_selected,students_input_str], 
            outputs=[output_box, btn_save, dummy_btn_1, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan, syllabus_actions_row, plan_buttons_row, output_plan_box]
        )
        
        # Edit Syllabus Button Click
        btn_edit_syl.click(
            enable_edit_syllabus_and_reload, 
            inputs=[course, output_box], 
            outputs=[output_box]
        )
        
        # Email Syllabus Button Click
        btn_email_syl.click(
            email_syllabus_callback, 
            inputs=[course, students_input_str, output_box], 
            outputs=[output_box] # Email callback now returns gr.update for output_box
        )
        
        # Generate Plan Button Click
        btn_generate_plan.click(
            generate_plan_callback, 
            inputs=[course_load_for_plan], 
            outputs=[output_plan_box, dummy_btn_2, dummy_btn_1, btn_generate_plan, dummy_btn_3, dummy_btn_4, btn_edit_plan, btn_email_plan]
        ).then( # .then() to ensure visibility updates happen *after* callback finishes
            lambda: (gr.update(visible=True), gr.update(visible=True)), 
            outputs=[output_plan_box, plan_buttons_row]
        )
        
        # Edit Plan Button Click
        btn_edit_plan.click(
            enable_edit_plan, 
            [], 
            [output_plan_box]
        )
        
        # Email Plan Button Click
        btn_email_plan.click(
            email_plan_callback, 
            inputs=[course_load_for_plan, students_input_str, output_plan_box], 
            outputs=[output_plan_box] # Email callback now returns gr.update for output_plan_box
        )
        
        # Link course name from Tab 1 to Tab 2
        course.change(lambda x: x, inputs=[course], outputs=[course_load_for_plan])

    return demo

# --- FastAPI Mounting & Main Execution ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup_event():
    scheduler.add_job(send_daily_class_reminders, trigger=CronTrigger(hour=5, minute=50, timezone='UTC'), id="daily_reminders", name="Daily Class Reminders", replace_existing=True)
    scheduler.add_job(check_student_progress_and_notify_professor, trigger=CronTrigger(hour=18, minute=0, timezone='UTC'), id="progress_check", name="Student Progress Check", replace_existing=True)
    if not scheduler.running: scheduler.start(); print("APScheduler started.")
    else: print("APScheduler already running.")
    for job in scheduler.get_jobs(): print(f"  Job: {job.id}, Name: {job.name}, Trigger: {job.trigger}")

@app.on_event("shutdown")
async def shutdown_event():
    if scheduler.running: scheduler.shutdown(); print("APScheduler shutdown.")

gradio_app_instance = build_ui()
app = gr.mount_gradio_app(app, gradio_app_instance, path="/")

@app.get("/healthz")
def healthz(): return {"status":"ok", "scheduler_running": scheduler.running}

if __name__ == "__main__":
    print("Starting Gradio UI locally. For production, use Uvicorn: uvicorn your_script_name:app --host 0.0.0.0 --port $PORT")
    # Use launch(share=True) for temporary public link if needed for testing
    gradio_app_instance.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT",7860)))
    # Keep main thread alive for scheduler when running locally without uvicorn
    try:
        while True:
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down scheduler...")
        if scheduler.running:
            scheduler.shutdown()
