import os
import io
import json
import traceback
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone as dt_timezone # Renamed to avoid conflict
import uuid # For generating unique student IDs
import random # For 5-digit code

import openai
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- New Imports ---
import jwt # PyJWT for access tokens
import requests # For calling external APIs (student progress)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# --- Configuration ---
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)

# SMTP Configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-super-secret-key-in-production")
if JWT_SECRET_KEY == "change-this-super-secret-key-in-production":
    print("WARNING: JWT_SECRET_KEY is set to its default insecure value. Please set a strong secret key in your environment variables.")
LINK_VALIDITY_HOURS = 6 # Link valid from 6 AM to 12 PM (6 hours)

# External API Configuration
EASYAI_TUTOR_PROGRESS_API_ENDPOINT = os.getenv("EASYAI_TUTOR_PROGRESS_API_ENDPOINT")

# Constants
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
            # Improved regex to catch more chapter variations
            for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad)\s+[\d\w]+.*", text):
                headings.append({"page": i + 1, "start_char_index": m.start(), "title": m.group().strip(), "page_index": i})
        
        # Sort by page then by character index on page
        headings.sort(key=lambda h: (h['page_index'], h['start_char_index']))
        
        sections = []
        if not headings: # If no headings found, treat whole PDF as one section
            full_content = "\n".join(pages_text)
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
                
                # Content from current heading's page
                if current_page_idx == next_page_idx:
                    content += pages_text[current_page_idx][start_char_on_page:end_char_on_page]
                else:
                    content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                    # Content from intermediate pages
                    for p_idx in range(current_page_idx + 1, next_page_idx):
                        content += pages_text[p_idx] + '\n'
                    # Content from next heading's page (up to start of next heading)
                    content += pages_text[next_page_idx][:end_char_on_page]
            else: # Last heading, take content till end of document
                content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                for p_idx in range(current_page_idx + 1, len(pages_text)):
                    content += pages_text[p_idx] + '\n'
            
            sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
        return sections

except ImportError:
    print("PyMuPDF (fitz) not found, falling back to PyPDF2. Section splitting will be basic.")
    from PyPDF2 import PdfReader
    def split_sections(pdf_file):
        # Ensure pdf_file is a file-like object or path for PdfReader
        if hasattr(pdf_file, "name"): # Gradio File object
            reader = PdfReader(pdf_file.name)
        elif isinstance(pdf_file, io.BytesIO): # BytesIO stream
             pdf_file.seek(0)
             reader = PdfReader(pdf_file)
        else: # Assuming it's a path string or Path object
            reader = PdfReader(str(pdf_file))

        text = "\n".join(page.extract_text() or '' for page in reader.pages)
        # Basic chunking if PyPDF2 is used
        chunks = re.split(r'(?<=[.?!])\s+', text)
        sections = []
        chunk_size = 10 # Number of sentences per "lesson"
        for i in range(0, len(chunks), chunk_size):
            title = f"Lesson Part {i//chunk_size+1}"
            content = " ".join(chunks[i:i+chunk_size]).strip()
            if content: # Only add if there's content
                sections.append({'title': title, 'content': content, 'page': None}) # Page info not easily available with PyPDF2 chunks
        if not sections and text.strip(): # If no chunks but text exists, add all text as one section
            sections.append({'title': 'Full Document Content', 'content': text.strip(), 'page': None})
        return sections

# --- Helpers ---
def download_docx(content, filename):
    buf = io.BytesIO()
    doc = Document()
    for line in content.split("\n"):
        doc.add_paragraph(line)
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
    # Ensure lesson_date_obj is a date object
    if isinstance(lesson_date_obj, str):
        lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()

    # Set issue time to 6 AM UTC of the lesson_date
    issue_datetime_utc = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    # Set expiration time
    expiration_datetime_utc = issue_datetime_utc + timedelta(hours=LINK_VALIDITY_HOURS)

    payload = {
        "sub": student_id,
        "course_id": course_id,
        "lesson_id": lesson_id_or_num,
        "iat": issue_datetime_utc,
        "exp": expiration_datetime_utc,
        "aud": "https://www.easyaitutor.com" # Audience claim
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
        "COURSE DESCRIPTION:", cfg['course_description'], "",
        "OBJECTIVES:"
    ] + objectives + [
        "", "GRADING & ASSESSMENTS:",
        " • Each class includes a quiz.",
        " • If score < 60%, student may retake the quiz next day.",
        " • Final grade = average of all quiz scores.",
        "", "SCHEDULE OVERVIEW:",
        f" • {mr}, every {', '.join(cfg['class_days'])}",
        "", "OFFICE HOURS & SUPPORT:",
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
    if not cfg.get('sections'):
        print("Warning: No sections found in config to generate lesson plan summaries.")
        # Handle case with no sections, perhaps by creating placeholder summaries
        for _ in class_dates:
             summaries.append("Topic to be determined based on class progression.")
    else:
        for sec_idx, sec in enumerate(cfg['sections']):
            # Only generate summaries if we have enough class dates for sections
            if sec_idx < len(class_dates):
                try:
                    resp = openai.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role":"system","content":"Summarize this section's main topic in one clear sentence suitable for a lesson plan."},
                            {"role":"user","content": sec['content'][:4000]} # Limit content to avoid token limits
                        ], temperature=0.7, max_tokens=80
                    )
                    summaries.append(resp.choices[0].message.content.strip())
                except Exception as e:
                    print(f"Error generating summary for section {sec_idx}: {e}")
                    summaries.append(f"Summary generation error for: {sec.get('title', 'Unknown Topic')}")
            else:
                # If more sections than class dates, we can't assign them all yet
                pass 
    
    # Ensure summaries list matches the number of class dates
    # If fewer summaries than dates, fill with placeholders
    while len(summaries) < len(class_dates):
        summaries.append("Topic to be announced or continued from previous lesson.")
    # If more summaries than dates (e.g. from too many sections), truncate summaries
    summaries = summaries[:len(class_dates)]


    lessons_by_week = {}
    structured_lessons = []

    for idx, dt_obj in enumerate(class_dates):
        week_number = dt_obj.isocalendar()[1]
        year_of_week = dt_obj.isocalendar()[0] # Important for multi-year courses
        week_key = f"{year_of_week}-W{week_number:02d}"


        summary_for_lesson = summaries[idx] if idx < len(summaries) else "Topic to be announced."
        
        page_num = None
        original_title = "N/A"
        if cfg.get('sections') and idx < len(cfg['sections']):
            page_num = cfg['sections'][idx].get('page')
            original_title = cfg['sections'][idx].get('title', 'N/A')

        lesson_data = {
            "lesson_number": idx + 1,
            "date": dt_obj.strftime('%Y-%m-%d'),
            "topic_summary": summary_for_lesson,
            "original_section_title": original_title,
            "page_reference": page_num
        }
        structured_lessons.append(lesson_data)
        
        lessons_by_week.setdefault(week_key, []).append(lesson_data)

    # Generate formatted plan string for UI display
    formatted_lines = []
    for week_key in sorted(lessons_by_week.keys()):
        # Extract year and week number for display
        year_disp, week_num_disp = week_key.split("-W")
        formatted_lines.append(f"## Week {week_num_disp} (Year {year_disp})\n")
        for lesson in lessons_by_week[week_key]:
            ds = datetime.strptime(lesson['date'], '%Y-%m-%d').strftime('%B %d, %Y')
            pstr = f" (Ref. p. {lesson['page_reference']})" if lesson['page_reference'] else ''
            formatted_lines.append(f"**Lesson {lesson['lesson_number']} ({ds}){pstr}:** {lesson['topic_summary']}")
        formatted_lines.append('')
    
    return "\n".join(formatted_lines), structured_lessons

# --- APScheduler Setup ---
scheduler = BackgroundScheduler(timezone="UTC") # Use UTC for consistency

# --- Scheduler Jobs ---
def send_daily_class_reminders():
    print(f"SCHEDULER: Running daily class reminder job at {datetime.now(dt_timezone.utc)}")
    today_utc = datetime.now(dt_timezone.utc).date()

    for config_file in CONFIG_DIR.glob("*_config.json"):
        try:
            cfg_text = config_file.read_text(encoding="utf-8")
            cfg = json.loads(cfg_text)
            
            course_id = config_file.stem.replace("_config", "")
            course_name = cfg.get("course_name", "N/A")
            
            if not cfg.get("lessons") or not cfg.get("students"):
                # print(f"SCHEDULER: Skipping {course_name}, missing lessons or students.")
                continue

            for lesson in cfg["lessons"]:
                lesson_date = datetime.strptime(lesson["date"], '%Y-%m-%d').date()
                if lesson_date == today_utc:
                    print(f"SCHEDULER: Class found for {course_name} today: Lesson {lesson['lesson_number']}")
                    class_code = generate_5_digit_code()

                    for student in cfg["students"]:
                        student_id = student.get("id", "unknown_student")
                        student_email = student.get("email")
                        student_name = student.get("name", "Student")

                        if not student_email:
                            print(f"SCHEDULER: Skipping student {student_name} (ID: {student_id}) due to missing email.")
                            continue

                        token = generate_access_token(
                            student_id,
                            course_id,
                            lesson["lesson_number"],
                            lesson_date # Pass the date object
                        )
                        access_link = f"https://www.easyaitutor.com/class?token={token}"
                        
                        email_subject = f"Today's Class Link for {course_name}: {lesson['topic_summary']}"
                        email_html_body = f"""
                        <html><head><style>body {{font-family: sans-serif;}} strong {{color: #007bff;}} a {{color: #0056b3; text-decoration: none;}} a:hover {{text-decoration: underline;}} .container {{padding: 20px; border: 1px solid #ddd; border-radius: 5px; max-width: 600px; margin: auto;}} .code {{font-size: 1.5em; font-weight: bold; background-color: #f0f0f0; padding: 5px 10px; border-radius: 3px; display: inline-block;}}</style></head>
                        <body><div class="container">
                            <p>Hi {student_name},</p>
                            <p>Your class for <strong>{course_name}</strong> - "{lesson['topic_summary']}" - is scheduled for today!</p>
                            <p>Please use the following link to access the AI Tutor session:</p>
                            <p><a href="{access_link}">{access_link}</a></p>
                            <p>You will also need this 5-digit code to begin: <span class="code">{class_code}</span></p>
                            <p>The link and code are valid from <strong>6:00 AM to 12:00 PM UTC</strong> today ({today_utc.strftime('%B %d, %Y')}).</p>
                            <p>Best regards,<br>Your AI Tutor System</p>
                        </div></body></html>
                        """
                        send_email_notification(student_email, email_subject, email_html_body, student_name)
        except json.JSONDecodeError as je:
            print(f"SCHEDULER: Error decoding JSON from {config_file.name}: {je}")
        except Exception as e:
            print(f"SCHEDULER: Error processing course config {config_file.name} for daily reminders: {e}\n{traceback.format_exc()}")

def check_student_progress_and_notify_professor():
    print(f"SCHEDULER: Running student progress check job at {datetime.now(dt_timezone.utc)}")
    if not EASYAI_TUTOR_PROGRESS_API_ENDPOINT:
        print("SCHEDULER: EASYAI_TUTOR_PROGRESS_API_ENDPOINT not configured. Skipping progress check.")
        return

    # Determine which lessons to check (e.g., those that occurred yesterday or recently)
    # For this example, let's check for lessons that occurred "yesterday" UTC
    yesterday_utc = datetime.now(dt_timezone.utc).date() - timedelta(days=1)

    for config_file in CONFIG_DIR.glob("*_config.json"):
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            course_id = config_file.stem.replace("_config", "")
            course_name = cfg.get("course_name", "N/A")
            instructor_cfg = cfg.get("instructor", {})
            instructor_email = instructor_cfg.get("email")
            instructor_name = instructor_cfg.get("name", "Instructor")

            if not instructor_email or not cfg.get("students") or not cfg.get("lessons"):
                continue

            for lesson in cfg.get("lessons", []):
                lesson_date = datetime.strptime(lesson["date"], '%Y-%m-%d').date()
                
                # Check progress for lessons that occurred on 'yesterday_utc'
                if lesson_date != yesterday_utc:
                    continue
                
                lesson_id_for_api = lesson["lesson_number"]
                print(f"SCHEDULER: Checking progress for {course_name}, Lesson {lesson_id_for_api} (Date: {lesson_date})")

                for student in cfg.get("students", []):
                    student_id = student.get("id")
                    student_name = student.get("name", "Student")

                    if not student_id: continue

                    try:
                        # --- API Call to easyaitutor.com ---
                        response = requests.get(
                            EASYAI_TUTOR_PROGRESS_API_ENDPOINT,
                            params={
                                "course_id": course_id,
                                "student_id": student_id,
                                "lesson_id": lesson_id_for_api
                                # Add API key/auth headers if required by easyaitutor.com
                                # "headers": {"Authorization": "Bearer YOUR_API_KEY_FOR_EASYAI_TUTOR"}
                            },
                            timeout=10 # Add a timeout
                        )
                        response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
                        progress_data = response.json()
                        
                        # Expected progress_data structure from easyaitutor.com:
                        # {
                        #   "quiz_score": 55, # Percentage (0-100)
                        #   "engagement_level": "low" | "medium" | "high", # or a numerical score
                        #   "time_spent_minutes": 20,
                        #   "details_url": "https://easyaitutor.com/progress/student/..." (optional)
                        # }

                        quiz_score = progress_data.get("quiz_score")
                        engagement = progress_data.get("engagement_level")

                        needs_attention = False
                        reasons = []
                        if quiz_score is not None and isinstance(quiz_score, (int, float)) and quiz_score < 60:
                            needs_attention = True
                            reasons.append(f"Quiz score was {quiz_score}% (below 60%)")
                        if isinstance(engagement, str) and engagement.lower() == "low":
                            needs_attention = True
                            reasons.append("Engagement was reported as low")
                        
                        if needs_attention:
                            print(f"SCHEDULER: Student {student_name} ({student_id}) in {course_name} needs attention for lesson {lesson_id_for_api}.")
                            subject = f"Student Progress Alert: {student_name} in {course_name}"
                            reasons_html = "".join([f"<li>{r}</li>" for r in reasons])
                            details_link_html = f"<p>More details (if available): <a href='{progress_data.get('details_url')}'>View Progress on EasyAITutor.com</a></p>" if progress_data.get('details_url') else ""
                            
                            body_html = f"""
                            <html><head><style>body {{font-family: sans-serif;}} strong {{color: #dc3545;}} .container {{padding: 20px; border: 1px solid #ddd; border-radius: 5px; max-width: 600px; margin: auto;}}</style></head>
                            <body><div class="container">
                                <p>Dear {instructor_name},</p>
                                <p>This is an alert regarding student <strong>{student_name}</strong>'s progress in your course: <strong>{course_name}</strong> for the lesson on "{lesson.get('topic_summary', 'Lesson ' + str(lesson_id_for_api))}" (Date: {lesson_date.strftime('%B %d, %Y')}).</p>
                                <p>Reasons for this alert:</p>
                                <ul>{reasons_html}</ul>
                                {details_link_html}
                                <p>You may want to engage with the student personally during your scheduled office hours or at your earliest convenience.</p>
                                <p>Best regards,<br>AI Tutor Monitoring System</p>
                            </div></body></html>
                            """
                            send_email_notification(instructor_email, subject, body_html, instructor_name)

                    except requests.exceptions.Timeout:
                        print(f"SCHEDULER: Timeout when fetching progress for {student_name}, {course_name}, lesson {lesson_id_for_api}.")
                    except requests.exceptions.RequestException as req_err:
                        print(f"SCHEDULER: API Error fetching progress for {student_name}, {course_name}, lesson {lesson_id_for_api}: {req_err}")
                    except json.JSONDecodeError:
                        print(f"SCHEDULER: Could not decode JSON response for student progress: {student_name}, {course_name}, lesson {lesson_id_for_api}. Response: {response.text[:200]}")
                    except Exception as e_prog:
                        print(f"SCHEDULER: Error processing progress for {student_name}: {e_prog}\n{traceback.format_exc()}")
        except json.JSONDecodeError as je:
            print(f"SCHEDULER: Error decoding JSON from {config_file.name} for progress check: {je}")
        except Exception as e_course:
            print(f"SCHEDULER: Error processing course config {config_file.name} for progress check: {e_course}\n{traceback.format_exc()}")


# --- All Gradio Callbacks ---
def save_setup(course_name, instr_name, instr_email, devices, pdf_file,
               sy, sm, sd_day, ey, em, ed_day, class_days_selected, students_input_str):
    # Expected outputs from btn_save.click:
    # 1. output_box (Tab 1)
    # 2. btn_save
    # 3. btn_show_syllabus_hidden (not directly used by user, for internal logic if any)
    # 4. btn_generate_plan (Tab 2)
    # 5. btn_edit_syl (Tab 1)
    # 6. btn_email_syl (Tab 1)
    # 7. btn_edit_plan (Tab 2)
    # 8. btn_email_plan (Tab 2)
    # 9. syllabus_actions_row (Tab 1)
    # 10. plan_buttons_row (Tab 2)
    # 11. output_plan_box (Tab 2)

    # Helper for error returns, ensuring all 11 components are addressed
    def error_return_tuple(error_message_str): # Function definition
        # THIS IS THE INDENTED BLOCK THAT WAS MISSING OR INCORRECTLY INDENTED
        return (
            gr.update(value=error_message_str, visible=True, interactive=False), # 1. output_box
            gr.update(visible=True),  # 2. btn_save (keep visible to allow retry)
            gr.update(visible=False), # 3. btn_show_syllabus_hidden
            gr.update(visible=False), # 4. btn_generate_plan
            gr.update(visible=False), # 5. btn_edit_syl
            gr.update(visible=False), # 6. btn_email_syl
            gr.update(visible=False), # 7. btn_edit_plan
            gr.update(visible=False), # 8. btn_email_plan
            gr.update(visible=False), # 9. syllabus_actions_row
            gr.update(visible=False), # 10. plan_buttons_row
            gr.update(value="", visible=False) # 11. output_plan_box
        ) # End of the indented block for error_return_tuple

    try:
        if not all([course_name, instr_name, instr_email, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected]):
            return error_return_tuple("⚠️ Error: All fields marked with * (Course Name, Instructor Name, Instructor Email, PDF, Start Date, End Date, Class Days) are required.")

        # Ensure pdf_file is processed correctly
        pdf_file_obj = pdf_file # Gradio passes a temp file object
        sections = split_sections(pdf_file_obj)
        if not sections:
             return error_return_tuple("⚠️ Error: Could not extract any sections from the PDF. Please check the PDF content and structure. If the PDF is valid, the section splitting logic might need adjustment for this document type.")

        full_content_for_ai = "\n\n".join(f"Title: {s['title']}\nContent: {s['content'][:1000]}" for s in sections) # Limit content for AI
        
        r1 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate a concise course description (2-3 sentences) based on the provided section titles and content snippets."},
                {"role":"user","content": full_content_for_ai}
            ]
        )
        desc = r1.choices[0].message.content.strip()
        
        r2 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate 5–10 clear, actionable learning objectives based on the provided section titles and content snippets. Start each with a verb."},
                {"role":"user","content": full_content_for_ai}
            ]
        )
        objs = [ln.strip(" -•*") for ln in r2.choices[0].message.content.splitlines() if ln.strip()]

        parsed_students = []
        for line in students_input_str.splitlines():
            if ',' in line:
                name, email_addr = line.split(',', 1)
                parsed_students.append({
                    "id": str(uuid.uuid4()),
                    "name": name.strip(),
                    "email": email_addr.strip()
                })
        
        cfg = {
            "course_name": course_name,
            "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days_selected,
            "start_date": f"{sy}-{sm}-{sd_day}",
            "end_date": f"{ey}-{em}-{ed_day}",
            "allowed_devices": devices, # Save allowed devices
            "students": parsed_students,
            "sections": sections, # Store the raw sections extracted
            "course_description": desc,
            "learning_objectives": objs,
            "lessons": [], # Initialize lessons, will be populated by generate_plan_callback
            "lesson_plan_formatted": "" # Initialize formatted plan
        }
        
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        
        syllabus_text = generate_syllabus(cfg)
        # Success case - return all 11 components
        return (
            gr.update(value=syllabus_text, visible=True, interactive=False), # 1. output_box
            gr.update(visible=False),  # 2. btn_save (hide after success)
            gr.update(visible=False),  # 3. btn_show_syllabus_hidden
            gr.update(visible=True),   # 4. btn_generate_plan (make available in Tab 2)
            gr.update(visible=True),   # 5. btn_edit_syl
            gr.update(visible=True),   # 6. btn_email_syl
            gr.update(visible=False),  # 7. btn_edit_plan (initially hidden in Tab 2)
            gr.update(visible=False),  # 8. btn_email_plan (initially hidden in Tab 2)
            gr.update(visible=True),   # 9. syllabus_actions_row (Tab 1)
            gr.update(visible=True),   # 10. plan_buttons_row (Tab 2)
            gr.update(value="", visible=False) # 11. output_plan_box (Tab 2, clear and hide initially)
        )
    except openai.APIError as oai_err:
        err_msg = f"⚠️ OpenAI API Error: {oai_err}. Check your API key and quota."
        print(err_msg + f"\n{traceback.format_exc()}")
        return error_return_tuple(err_msg)
    except Exception:
        err = f"⚠️ Error during setup:\n{traceback.format_exc()}"
        print(err)
        return error_return_tuple(err)

# The next function, show_syllabus_callback, should start immediately after this.

def show_syllabus_callback(course_name):
    try:
        if not course_name: return (gr.update(value="⚠️ Error: Course Name is required to show syllabus.", visible=True, interactive=False),) + (None,)*7
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return (gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.", visible=True, interactive=False),) + (None,)*7
        
        cfg = json.loads(path.read_text(encoding="utf-8"))
        syllabus = generate_syllabus(cfg)
        has_plan = bool(cfg.get("lessons")) # Check if structured lessons exist
        
        return (
            gr.update(value=syllabus, visible=True, interactive=False),
            gr.update(visible=False), # Save setup
            gr.update(visible=False), # Show syllabus (this button itself)
            gr.update(visible=True),  # Generate/Show Plan
            gr.update(visible=True),  # Edit Syllabus
            gr.update(visible=True),  # Email Syllabus
            gr.update(visible=has_plan), # Edit Plan (visible if plan exists)
            gr.update(visible=has_plan)  # Email Plan (visible if plan exists)
        )
    except Exception:
        err = f"⚠️ Error showing syllabus:\n{traceback.format_exc()}"
        print(err)
        return (gr.update(value=err, visible=True, interactive=False),) + (None,)*7


def generate_plan_callback(course_name): # Removed date inputs as they are in config
    try:
        if not course_name: return (gr.update(value="⚠️ Error: Course Name is required.", visible=True, interactive=False),) + (None,)*7
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return (gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.", visible=True, interactive=False),) + (None,)*7

        cfg = json.loads(path.read_text(encoding="utf-8"))
        
        # Generate plan if not already generated or if forced
        # For simplicity, let's always regenerate if this button is clicked,
        # or check a flag if you want to avoid re-calling OpenAI.
        # Here, we assume it's okay to regenerate.
        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg)
        
        cfg["lessons"] = structured_lessons_list
        cfg["lesson_plan_formatted"] = formatted_plan_str # Store the display string too
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        
        return (
            gr.update(value=formatted_plan_str, visible=True, interactive=False),
            gr.update(visible=False),  # Save setup
            gr.update(visible=True),   # Show Syllabus
            gr.update(visible=False),  # Generate/Show Plan (this button)
            gr.update(visible=False),  # Edit Syllabus
            gr.update(visible=False),  # Email Syllabus
            gr.update(visible=True),   # Edit Lesson Plan
            gr.update(visible=True)    # Email Lesson Plan
        )
    except openai.APIError as oai_err:
        err_msg = f"⚠️ OpenAI API Error during plan generation: {oai_err}. Check your API key and quota."
        print(err_msg + f"\n{traceback.format_exc()}")
        return (gr.update(value=err_msg, visible=True, interactive=False),) + (None,)*7
    except Exception:
        err = f"⚠️ Error generating lesson plan:\n{traceback.format_exc()}"
        print(err)
        return (gr.update(value=err, visible=True, interactive=False),) + (None,)*7

def show_lesson_plan_callback(course_name): # New callback to just show existing plan
    try:
        if not course_name: return (gr.update(value="⚠️ Error: Course Name is required.", visible=True, interactive=False),) + (None,)*7
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return (gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.", visible=True, interactive=False),) + (None,)*7

        cfg = json.loads(path.read_text(encoding="utf-8"))
        plan_str = cfg.get("lesson_plan_formatted", "Lesson plan not generated yet. Click 'Generate Lesson Plan'.")
        has_plan = bool(cfg.get("lessons"))

        return (
            gr.update(value=plan_str, visible=True, interactive=False),
            gr.update(visible=False),  # Save setup
            gr.update(visible=True),   # Show Syllabus
            gr.update(visible=False),  # Generate/Show Plan (this button)
            gr.update(visible=False),  # Edit Syllabus
            gr.update(visible=False),  # Email Syllabus
            gr.update(visible=has_plan),   # Edit Lesson Plan
            gr.update(visible=has_plan)    # Email Lesson Plan
        )
    except Exception:
        err = f"⚠️ Error showing lesson plan:\n{traceback.format_exc()}"
        print(err)
        return (gr.update(value=err, visible=True, interactive=False),) + (None,)*7


def enable_edit_syllabus(): return gr.update(interactive=True)
def enable_edit_plan(): return gr.update(interactive=True)

# Note: If syllabus/plan is edited in UI, those edits are NOT saved back to JSON by default.
# This would require additional "Save Edited Syllabus/Plan" buttons and logic.

def email_document_callback(course_name, doc_type, output_text_content, students_input_str):
    # Generic emailer for syllabus or plan
    if not SMTP_USER or not SMTP_PASS:
        return f"⚠️ Error: SMTP settings (User/Pass) not configured. Cannot send email."
    try:
        if not course_name or not output_text_content:
            return f"⚠️ Error: Course Name and {doc_type} content are required."

        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return f"⚠️ Error: Config for '{course_name}' not found."
        cfg = json.loads(path.read_text(encoding="utf-8"))
        
        instr_name = cfg.get("instructor", {}).get("name", "Instructor")
        instr_email = cfg.get("instructor", {}).get("email")

        buf, fn = download_docx(output_text_content, f"{course_name.replace(' ','_')}_{doc_type.lower()}.docx")
        attachment_data = buf.read()
        
        # Parse student string from UI (might be different from stored if edited)
        # Or use stored students: cfg.get("students", [])
        recipients = []
        if instr_email: recipients.append({"name": instr_name, "email": instr_email})

        for line in students_input_str.splitlines():
            if ',' in line:
                name, email_addr = line.split(',', 1)
                recipients.append({"name": name.strip(), "email": email_addr.strip()})
        
        if not recipients:
            return f"⚠️ Error: No recipients found (neither instructor nor students provided/valid)."

        success_count = 0
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
                print(f"SMTP Error sending to {rec['email']}: {e_smtp}")
        
        return f"✅ {doc_type.capitalize()} emailed to {success_count} recipient(s)."

    except Exception:
        return f"⚠️ Error emailing {doc_type.lower()}:\n{traceback.format_exc()}"

def email_syllabus_callback(course_name, students_input_str, output_box_content):
    return email_document_callback(course_name, "Syllabus", output_box_content, students_input_str)

def email_plan_callback(course_name, students_input_str, output_box_content):
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
                    with gr.Column():
                        gr.Markdown("#### Course Schedule")
                        years    = [str(y) for y in range(datetime.now().year, datetime.now().year + 5)]
                        months   = [f"{m:02d}" for m in range(1,13)]
                        days     = [f"{d:02d}" for d in range(1,32)]
                        with gr.Row():
                            sy = gr.Dropdown(years, label="Start Year*")
                            sm = gr.Dropdown(months, label="Start Month*")
                            sd_day = gr.Dropdown(days,   label="Start Day*")
                        with gr.Row():
                            ey = gr.Dropdown(years, label="End Year*")
                            em = gr.Dropdown(months, label="End Month*")
                            ed_day = gr.Dropdown(days,   label="End Day*")
                        class_days_selected = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*")
                    with gr.Column():
                        gr.Markdown("#### Student & Access")
                        devices  = gr.CheckboxGroup(["Phone","PC", "Tablet"], label="Allowed Devices for AI Tutor", value=["PC"])
                        students_input_str = gr.Textbox(label="Students (Name,Email per line)", lines=5, placeholder="Student One,student1@example.com\nStudent Two,student2@example.com")

                btn_save = gr.Button("1. Save Setup & Generate Syllabus", variant="primary")
                
                gr.Markdown("---") # Separator
                output_box = gr.Textbox(label="Output (Syllabus / Lesson Plan / Status)", lines=20, interactive=False, visible=False, show_copy_button=True)
                
                with gr.Row(visible=False) as syllabus_actions_row:
                    # Corrected Button Definitions for Syllabus Actions
                    btn_edit_syl  = gr.Button(value="📝 Edit Syllabus Text") 
                    btn_email_syl = gr.Button(value="📧 Email Syllabus", variant="secondary")
                
                # Hidden button (can be removed if not actively used for a specific trigger)
                btn_show_syllabus_hidden = gr.Button("Show Existing Syllabus", visible=False) 

            with gr.TabItem("Lesson Plan Management"):
                gr.Markdown("Load a course first or complete setup on the 'Course Setup' tab.")
                course_load_for_plan = gr.Textbox(label="Enter Course Name to Load/Manage Plan", placeholder="Type existing course name and press Enter or click Load")
                btn_load_course_for_plan = gr.Button("Load Course for Plan Management")

                output_plan_box = gr.Textbox(label="Lesson Plan Output", lines=20, interactive=False, visible=False, show_copy_button=True)

                with gr.Row(visible=False) as plan_buttons_row:
                    btn_generate_plan = gr.Button("2. Generate/Re-generate Lesson Plan", variant="primary")
                    # Corrected Button Definitions for Plan Actions
                    btn_edit_plan = gr.Button(value="📝 Edit Plan Text")
                    btn_email_plan= gr.Button(value="📧 Email Lesson Plan", variant="secondary")

        # --- Event Handlers ---
        # Tab 1: Course Setup & Syllabus
        btn_save.click(
            save_setup,
            inputs=[course,instr,email,devices,pdf_file,sy,sm,sd_day,ey,em,ed_day,class_days_selected,students_input_str],
            outputs=[output_box, btn_save, btn_show_syllabus_hidden, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan, syllabus_actions_row, plan_buttons_row, output_plan_box]
        ).then(
            lambda: (gr.update(visible=True), gr.update(visible=True)), outputs=[output_box, syllabus_actions_row] # Show output & syllabus actions
        )

        btn_edit_syl.click(enable_edit_syllabus, [], [output_box])
        btn_email_syl.click(
            email_syllabus_callback,
            inputs=[course, students_input_str, output_box], 
            outputs=[output_box] 
        )

        # Tab 2: Lesson Plan Management
        def load_course_for_plan_ui(course_name_input):
            try:
                if not course_name_input: 
                    return (
                        gr.update(value="Enter a course name to load.", visible=True), # output_box (Tab 1)
                        gr.update(visible=False), # syllabus_actions_row (Tab 1)
                        gr.update(visible=False), # plan_buttons_row (Tab 2)
                        gr.update(value="", visible=False), # output_plan_box (Tab 2)
                        gr.update(visible=False), # btn_edit_plan
                        gr.update(visible=False)  # btn_email_plan
                    )
                
                path = CONFIG_DIR / f"{course_name_input.replace(' ','_').lower()}_config.json"
                if not path.exists(): 
                    return (
                        gr.update(value=f"Config for '{course_name_input}' not found.", visible=True), 
                        gr.update(visible=False), 
                        gr.update(visible=False), 
                        gr.update(value="", visible=False),
                        gr.update(visible=False),
                        gr.update(visible=False)
                    )

                cfg = json.loads(path.read_text(encoding="utf-8"))
                syllabus_text = generate_syllabus(cfg)
                plan_text = cfg.get("lesson_plan_formatted", "Lesson plan not generated yet. Click 'Generate/Re-generate Lesson Plan'.")
                has_plan_data = bool(cfg.get("lessons")) # Check if structured lessons exist

                return (
                    gr.update(value=syllabus_text, visible=True), 
                    gr.update(visible=True), 
                    gr.update(visible=True), 
                    gr.update(value=plan_text, visible=True),
                    gr.update(visible=has_plan_data), 
                    gr.update(visible=has_plan_data)
                )
            except Exception as e:
                tb_str = traceback.format_exc()
                print(f"Error loading course for plan UI: {e}\n{tb_str}")
                return (
                    gr.update(value=f"Error loading course: {e}", visible=True), 
                    gr.update(visible=False), 
                    gr.update(visible=False), 
                    gr.update(value="", visible=False),
                    gr.update(visible=False),
                    gr.update(visible=False)
                )

        btn_load_course_for_plan.click(
            load_course_for_plan_ui,
            inputs=[course_load_for_plan],
            outputs=[output_box, syllabus_actions_row, plan_buttons_row, output_plan_box, btn_edit_plan, btn_email_plan]
        )

        btn_generate_plan.click(
            generate_plan_callback,
            inputs=[course_load_for_plan], 
            outputs=[output_plan_box, btn_save, btn_show_syllabus_hidden, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan]
        ).then(
            lambda: (gr.update(visible=True), gr.update(visible=True)), outputs=[output_plan_box, plan_buttons_row]
        )
        
        btn_edit_plan.click(enable_edit_plan, [], [output_plan_box])
        btn_email_plan.click(
            email_plan_callback,
            inputs=[course_load_for_plan, students_input_str, output_plan_box], 
            outputs=[output_plan_box]
        )
        
        # Link course name input from Tab 1 to Tab 2 for convenience when user types in Tab 1
        course.change(lambda x: x, inputs=[course], outputs=[course_load_for_plan])

    return demo

        # --- Event Handlers ---
        # Tab 1: Course Setup & Syllabus
        btn_save.click(
            save_setup,
            inputs=[course,instr,email,devices,pdf_file,sy,sm,sd_day,ey,em,ed_day,class_days_selected,students_input_str],
            outputs=[output_box, btn_save, btn_show_syllabus_hidden, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan, syllabus_actions_row, plan_buttons_row, output_plan_box]
        ).then(
            lambda: (gr.update(visible=True), gr.update(visible=True)), outputs=[output_box, syllabus_actions_row] # Show output & syllabus actions
        )

        # Logic to show syllabus if course name is entered and config exists (simplified)
        # This is a bit tricky with Gradio's event model for just typing.
        # A "Load Course" button might be better. For now, btn_show_syllabus_hidden is a placeholder.

        btn_edit_syl.click(enable_edit_syllabus, [], [output_box])
        btn_email_syl.click(
            email_syllabus_callback,
            inputs=[course, students_input_str, output_box], # Use current course name and student list from UI
            outputs=[output_box] # Display status in the same box
        )

        # Tab 2: Lesson Plan Management
        def load_course_for_plan_ui(course_name_input):
            # This function will try to load the course and update UI elements
            # It will show the syllabus in output_box and plan in output_plan_box if they exist
            try:
                if not course_name_input: 
                    return (gr.update(value="Enter a course name.", visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(value="", visible=False))
                
                path = CONFIG_DIR / f"{course_name_input.replace(' ','_').lower()}_config.json"
                if not path.exists(): 
                    return (gr.update(value=f"Config for '{course_name_input}' not found.", visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(value="", visible=False))

                cfg = json.loads(path.read_text(encoding="utf-8"))
                syllabus_text = generate_syllabus(cfg)
                plan_text = cfg.get("lesson_plan_formatted", "Lesson plan not generated yet.")
                has_plan = bool(cfg.get("lessons"))

                # Update main output box (from Tab 1) with syllabus, and plan box with plan
                # Also update visibility of action buttons
                return (
                    gr.update(value=syllabus_text, visible=True), # output_box (Tab 1)
                    gr.update(visible=True), # syllabus_actions_row (Tab 1)
                    gr.update(visible=True), # plan_buttons_row (Tab 2)
                    gr.update(value=plan_text, visible=True), # output_plan_box (Tab 2)
                    gr.update(visible=has_plan), # btn_edit_plan
                    gr.update(visible=has_plan)  # btn_email_plan
                )
            except Exception as e:
                tb_str = traceback.format_exc()
                print(f"Error loading course for plan UI: {e}\n{tb_str}")
                return (gr.update(value=f"Error loading course: {e}", visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(value="", visible=False))


        btn_load_course_for_plan.click(
            load_course_for_plan_ui,
            inputs=[course_load_for_plan],
            outputs=[output_box, syllabus_actions_row, plan_buttons_row, output_plan_box, btn_edit_plan, btn_email_plan]
        )

        btn_generate_plan.click(
            generate_plan_callback,
            inputs=[course_load_for_plan], # Use the course name from Tab 2 input
            outputs=[output_plan_box, btn_save, btn_show_syllabus_hidden, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan]
        ).then(
            lambda: (gr.update(visible=True), gr.update(visible=True)), outputs=[output_plan_box, plan_buttons_row]
        )
        
        btn_edit_plan.click(enable_edit_plan, [], [output_plan_box])
        btn_email_plan.click(
            email_plan_callback,
            inputs=[course_load_for_plan, students_input_str, output_plan_box], # Use course name from Tab 2, students from Tab 1
            outputs=[output_plan_box]
        )
        
        # Link course name input from Tab 1 to Tab 2 for convenience
        course.change(lambda x: x, inputs=[course], outputs=[course_load_for_plan])


    return demo

# --- FastAPI Mounting & Main Execution ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    # Schedule daily class reminders (e.g., at 05:50 AM UTC for 6 AM links)
    # APScheduler uses local time of the server if timezone is not specified for the job,
    # but BackgroundScheduler was initialized with UTC, so cron times are UTC.
    scheduler.add_job(
        send_daily_class_reminders,
        trigger=CronTrigger(hour=5, minute=50, timezone='UTC'), # 5:50 AM UTC
        id="daily_class_reminders_job",
        name="Daily Class Reminders",
        replace_existing=True
    )
    
    # Schedule student progress check (e.g., daily at 18:00 UTC / 6 PM UTC)
    scheduler.add_job(
        check_student_progress_and_notify_professor,
        trigger=CronTrigger(hour=18, minute=0, timezone='UTC'), # 6:00 PM UTC
        id="student_progress_check_job",
        name="Student Progress Check",
        replace_existing=True
    )
    if not scheduler.running:
        scheduler.start()
        print("APScheduler started with jobs.")
    else:
        print("APScheduler already running.")
    # Print scheduled jobs
    print("Scheduled jobs:")
    for job in scheduler.get_jobs():
        print(f"  Job ID: {job.id}, Name: {job.name}, Trigger: {job.trigger}")


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler.running:
        scheduler.shutdown()
        print("APScheduler shutdown.")

gradio_app_instance = build_ui()
app = gr.mount_gradio_app(app, gradio_app_instance, path="/")

@app.get("/healthz")
def healthz():
    return {"status":"ok", "scheduler_running": scheduler.running}

if __name__ == "__main__":
    # This part is for local execution. When deployed via Uvicorn/Gunicorn,
    # the FastAPI app `app` is the entry point.
    # The scheduler startup is handled by FastAPI @app.on_event("startup").
    
    # For local testing, you might want to start the scheduler here if not using FastAPI events directly.
    # However, with FastAPI events, it's cleaner.
    # If you run `python your_script_name.py`, Gradio's launch will start its own server.
    # To run with Uvicorn (recommended for FastAPI):
    # uvicorn your_script_name:app --reload --port 7860
    
    print("Starting Gradio UI locally (not using Uvicorn for this __main__ block)...")
    print("For production or full FastAPI features, run with: uvicorn your_script_name:app --host 0.0.0.0 --port YOUR_PORT")
    
    # Manually start scheduler if not running via FastAPI startup (e.g. direct .launch())
    if not scheduler.running:
        # Call the startup logic here if needed for direct launch
        # This is a bit redundant if FastAPI events are used and you run with uvicorn.
        # For simplicity, we'll rely on FastAPI events.
        pass

    gradio_app_instance.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT",7860)))

    # Ensure scheduler is shutdown on exit if launched directly
    try:
        while True:
            time.sleep(2) # Keep main thread alive for scheduler
    except (KeyboardInterrupt, SystemExit):
        if scheduler.running:
            scheduler.shutdown()
            print("APScheduler shutdown on script exit.")
