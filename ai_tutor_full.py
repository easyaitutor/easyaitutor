import os
import io
import json
import traceback
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone as dt_timezone
import uuid
import random
import time

import openai
import gradio as gr
from docx import Document # For creating DOCX files
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import jwt # PyJWT for access tokens
import requests # For calling external APIs (student progress)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Attempt to import fitz (PyMuPDF)
try:
    import fitz
    fitz_available = True
except ImportError:
    fitz_available = False
    print("PyMuPDF (fitz) not found. Page number mapping for lesson plans will be limited or unavailable.")

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

# --- PDF loader & Section Splitter (Primarily for Course Desc/Objectives) ---
def split_sections(pdf_file_obj_for_sections):
    """
    Splits PDF into structural sections based on headings.
    Used for generating overall course description and learning objectives.
    """
    if fitz_available:
        try:
            doc = None
            if hasattr(pdf_file_obj_for_sections, "name"): # Gradio File object
                doc = fitz.open(pdf_file_obj_for_sections.name)
            elif hasattr(pdf_file_obj_for_sections, "read"): # BytesIO or similar
                pdf_bytes_sec = pdf_file_obj_for_sections.read()
                pdf_file_obj_for_sections.seek(0) # Reset pointer
                doc = fitz.open(stream=pdf_bytes_sec, filetype="pdf")

            if not doc:
                raise Exception("Could not open PDF with fitz for section splitting.")

            pages_text = [page.get_text() for page in doc]
            doc.close()
            headings = []
            for i, text in enumerate(pages_text):
                for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad|Part|Module)\s+[\d\w]+.*", text):
                    headings.append({"page": i + 1, "start_char_index": m.start(), "title": m.group().strip(), "page_index": i})
            
            headings.sort(key=lambda h: (h['page_index'], h['start_char_index']))
            
            sections = []
            if not headings:
                full_content = "\n".join(pages_text)
                if full_content.strip():
                     sections.append({'title': 'Full Document Content', 'content': full_content.strip(), 'page': 1})
                return sections

            for idx, h in enumerate(headings):
                current_page_idx, start_char_on_page = h['page_index'], h['start_char_index']
                content = ''
                if idx + 1 < len(headings):
                    next_h = headings[idx+1]
                    next_page_idx, end_char_on_page = next_h['page_index'], next_h['start_char_index']
                    if current_page_idx == next_page_idx:
                        content += pages_text[current_page_idx][start_char_on_page:end_char_on_page]
                    else:
                        content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                        for p_idx in range(current_page_idx + 1, next_page_idx): content += pages_text[p_idx] + '\n'
                        content += pages_text[next_page_idx][:end_char_on_page]
                else:
                    content += pages_text[current_page_idx][start_char_on_page:] + '\n'
                    for p_idx in range(current_page_idx + 1, len(pages_text)): content += pages_text[p_idx] + '\n'
                if content.strip():
                    sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
            
            sections = [s for s in sections if len(s['content']) > len(s['title']) + 20] # Heuristic
            print(f"DEBUG: split_sections (fitz) found {len(headings)} headings, returning {len(sections)} usable sections for desc/obj.")
            return sections
        except Exception as e_fitz:
            print(f"Error during fitz section splitting: {e_fitz}. Falling back if PyPDF2 available.")
            # Fall through to PyPDF2 if fitz fails but was initially available

    # Fallback to PyPDF2 if fitz is not available or failed
    try:
        from PyPDF2 import PdfReader
        print("Using PyPDF2 for section splitting (basic).")
        if hasattr(pdf_file_obj_for_sections, "name"):
            reader = PdfReader(pdf_file_obj_for_sections.name)
        elif isinstance(pdf_file_obj_for_sections, io.BytesIO):
             pdf_file_obj_for_sections.seek(0)
             reader = PdfReader(pdf_file_obj_for_sections)
        else: # Assuming path string
            reader = PdfReader(str(pdf_file_obj_for_sections))

        text = "\n".join(page.extract_text() or '' for page in reader.pages)
        chunks = re.split(r'(?<=[.?!])\s+', text) # Split by sentences
        sections = []
        # Group N sentences into a "section" for PyPDF2
        sentences_per_section_pypdf2 = 15 
        for i in range(0, len(chunks), sentences_per_section_pypdf2):
            title = f"Content Block {i//sentences_per_section_pypdf2+1}"
            content = " ".join(chunks[i:i+sentences_per_section_pypdf2]).strip()
            if content:
                sections.append({'title': title, 'content': content, 'page': None}) # Page info hard with PyPDF2
        if not sections and text.strip(): # If no chunks but text exists
            sections.append({'title': 'Full Document Content (PyPDF2)', 'content': text.strip(), 'page': None})
        print(f"DEBUG: split_sections (PyPDF2) created {len(sections)} basic sections for desc/obj.")
        return sections
    except ImportError:
        print("PyPDF2 not found. Cannot split PDF for structural sections.")
        return [{'title': 'PDF Processing Error', 'content': 'Could not process PDF for sections.', 'page': None}]
    except Exception as e_pypdf2:
        print(f"Error during PyPDF2 section splitting: {e_pypdf2}")
        return [{'title': 'PDF Processing Error', 'content': f'Error splitting with PyPDF2: {e_pypdf2}', 'page': None}]


# --- Helpers ---
def download_docx(content, filename):
    buf = io.BytesIO()
    doc = Document()
    for line in content.split("\n"):
        paragraph = doc.add_paragraph()
        parts = re.split(r'(\*\*.*?\*\*)', line) 
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
        if cur.weekday() in wdays: cnt += 1
        cur += timedelta(days=1)
    return cnt

def generate_access_token(student_id, course_id, lesson_id_or_num, lesson_date_obj):
    if isinstance(lesson_date_obj, str): lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()
    issue_datetime_utc = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    expiration_datetime_utc = issue_datetime_utc + timedelta(hours=LINK_VALIDITY_HOURS)
    payload = {"sub": student_id, "course_id": course_id, "lesson_id": lesson_id_or_num, "iat": issue_datetime_utc, "exp": expiration_datetime_utc, "aud": "https://www.easyaitutor.com"}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

def generate_5_digit_code(): return str(random.randint(10000, 99999))

def send_email_notification(to_email, subject, html_content, student_name="User"):
    if not SMTP_USER or not SMTP_PASS: print(f"SMTP credentials not set. Cannot send email to {to_email}"); return False
    msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, to_email
    msg.add_alternative(html_content, subtype='html')
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s: s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print(f"Email sent to {to_email} with subject: {subject}"); return True
    except Exception as e: print(f"Failed to send email to {to_email}: {e}\n{traceback.format_exc()}"); return False

# --- Generate Syllabus ---
def generate_syllabus(cfg):
    sd, ed = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date(), datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    mr, total = f"{sd.strftime('%B')}–{ed.strftime('%B')}", count_classes(sd, ed, [days_map[d] for d in cfg['class_days']])
    header = [f"Course Name: {cfg['course_name']}", f"Professor:   {cfg['instructor']['name']}", f"Email:       {cfg['instructor']['email']}", f"Duration:    {mr} ({total} classes)", '_'*60]
    objectives = [f" • {o}" for o in cfg['learning_objectives']]
    body = ["COURSE DESCRIPTION:", cfg['course_description'], "", "OBJECTIVES:"] + objectives + ["", "GRADING & ASSESSMENTS:", " • Each class includes a quiz.", " • If score < 60%, student may retake quiz next day.", " • Final grade = average of all quiz scores.", "", "SCHEDULE OVERVIEW:", f" • {mr}, every {', '.join(cfg['class_days'])}", "", "OFFICE HOURS & SUPPORT:", " • Office Hours: Tuesdays 3–5 PM; Thursdays 10–11 AM (Zoom)", " • Email response within 24 hours on weekdays"]
    return "\n".join(header + [""] + body)

# --- Generate Lesson Plan (New Strategy: Character-based Segmentation) ---
def generate_plan_by_week_structured_and_formatted(cfg):
    sd, ed = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date(), datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    wdays = {days_map[d] for d in cfg['class_days']}
    class_dates = [cur_dt for cur_dt in (sd + timedelta(days=i) for i in range((ed - sd).days + 1)) if cur_dt.weekday() in wdays]
    print(f"DEBUG: Number of class dates calculated: {len(class_dates)}")
    if not class_dates: return "No class dates scheduled. Cannot generate lesson plan.", []

    full_text = cfg.get("full_text_content", "")
    char_page_map = cfg.get("char_offset_page_map", [])

    if not full_text.strip():
        print("Warning: Full text content is empty. Cannot generate lesson plan topics from PDF content.")
        placeholder_lessons, placeholder_formatted_lines, weeks_for_placeholder = [], [], {}
        for idx, dt_obj in enumerate(class_dates):
            wk_key = f"{dt_obj.isocalendar()[0]}-W{dt_obj.isocalendar()[1]:02d}"
            lesson_data = {"lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'), "topic_summary": "Topic to be determined (No PDF text)", "original_section_title": "N/A", "page_reference": None}
            placeholder_lessons.append(lesson_data); weeks_for_placeholder.setdefault(wk_key, []).append(lesson_data)
        for wk_key in sorted(weeks_for_placeholder.keys()):
            yr_disp, wk_num_disp = wk_key.split("-W")
            placeholder_formatted_lines.append(f"**Week {wk_num_disp} (Year {yr_disp})**\n")
            for lesson in weeks_for_placeholder[wk_key]: placeholder_formatted_lines.append(f"**Lesson {lesson['lesson_number']} ({datetime.strptime(lesson['date'], '%Y-%m-%d').strftime('%B %d, %Y')})**: {lesson['topic_summary']}")
            placeholder_formatted_lines.append('')
        return "\n".join(placeholder_formatted_lines), placeholder_lessons

    total_chars, num_lessons = len(full_text), len(class_dates)
    chars_per_lesson = total_chars // num_lessons if num_lessons > 0 else total_chars
    min_chars_for_summary = 150 
    print(f"DEBUG: Total chars: {total_chars}, Chars per lesson: {chars_per_lesson}")

    summaries, current_char_ptr, segment_start_offsets = [], 0, []
    for i in range(num_lessons):
        segment_start_offsets.append(current_char_ptr)
        start, end = current_char_ptr, (current_char_ptr + chars_per_lesson if i < num_lessons - 1 else total_chars)
        segment_text, current_char_ptr = full_text[start:end].strip(), end
        if len(segment_text) < min_chars_for_summary:
            print(f"DEBUG: Segment {i+1} too short ({len(segment_text)} chars), using 'Review'.")
            summaries.append("Review or brief topic discussion.")
        else:
            try:
                print(f"DEBUG: Summarizing segment {i+1} (len {len(segment_text)}): '{segment_text[:70].replace(chr(10),' ')}...'")
                resp = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"You are an AI creating a lesson plan topic. Identify the single core concept/skill in the text. Respond ONLY with a short, concise phrase (max 10-12 words) as a lesson topic title, preferably starting with a gerund (e.g., 'Using verbs'). Do NOT use a full sentence."}, {"role":"user","content": segment_text}], temperature=0.4, max_tokens=30)
                summary = resp.choices[0].message.content.strip().replace('"', '').capitalize()
                summaries.append(summary)
            except Exception as e: print(f"Error summarizing segment {i+1}: {e}"); summaries.append(f"Topic for segment {i+1} (Summary Error)")
    
    lessons_by_week, structured_lessons = {}, []
    for idx, dt_obj in enumerate(class_dates):
        wk_key = f"{dt_obj.isocalendar()[0]}-W{dt_obj.isocalendar()[1]:02d}"
        summary = summaries[idx]
        est_page_num = None
        if char_page_map:
            seg_start_offset = segment_start_offsets[idx]
            for offset, pg_num in reversed(char_page_map):
                if seg_start_offset >= offset: est_page_num = pg_num; break
            if est_page_num is None and char_page_map: est_page_num = char_page_map[0][1]
        lesson_data = {"lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'), "topic_summary": summary, "original_section_title": f"Text Segment {idx+1}", "page_reference": est_page_num}
        structured_lessons.append(lesson_data); lessons_by_week.setdefault(wk_key, []).append(lesson_data)

    formatted_lines = []
    for wk_key in sorted(lessons_by_week.keys()):
        yr_disp, wk_num_disp = wk_key.split("-W")
        formatted_lines.append(f"**Week {wk_num_disp} (Year {yr_disp})**\n")
        for lesson in lessons_by_week[wk_key]:
            ds = datetime.strptime(lesson['date'], '%Y-%m-%d').strftime('%B %d, %Y')
            pstr = f" (Approx. Ref. p. {lesson['page_reference']})" if lesson['page_reference'] else ''
            formatted_lines.append(f"**Lesson {lesson['lesson_number']} ({ds})**{pstr}: {lesson['topic_summary']}")
        formatted_lines.append('')
    return "\n".join(formatted_lines), structured_lessons

# --- APScheduler Setup & Jobs (Identical to previous full version) ---
scheduler = BackgroundScheduler(timezone="UTC")
# ... (send_daily_class_reminders and check_student_progress_and_notify_professor functions are unchanged) ...
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
    if not EASYAI_TUTOR_PROGRESS_API_ENDPOINT: print("SCHEDULER: EASYAI_TUTOR_PROGRESS_API_ENDPOINT not set. Skipping."); return
    yesterday_utc = datetime.now(dt_timezone.utc).date() - timedelta(days=1)
    for config_file in CONFIG_DIR.glob("*_config.json"):
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            course_id, course_name = config_file.stem.replace("_config", ""), cfg.get("course_name", "N/A")
            instructor_cfg = cfg.get("instructor", {}); instructor_email, instructor_name = instructor_cfg.get("email"), instructor_cfg.get("name", "Instructor")
            if not instructor_email or not cfg.get("students") or not cfg.get("lessons"): continue
            for lesson in cfg.get("lessons", []):
                lesson_date = datetime.strptime(lesson["date"], '%Y-%m-%d').date()
                if lesson_date != yesterday_utc: continue
                lesson_id_for_api = lesson["lesson_number"]; print(f"SCHEDULER: Checking progress for {course_name}, Lesson {lesson_id_for_api}")
                for student in cfg.get("students", []):
                    student_id, student_name = student.get("id"), student.get("name", "Student")
                    if not student_id: continue
                    try:
                        response = requests.get(EASYAI_TUTOR_PROGRESS_API_ENDPOINT, params={"course_id": course_id, "student_id": student_id, "lesson_id": lesson_id_for_api}, timeout=10)
                        response.raise_for_status(); progress_data = response.json()
                        quiz_score, engagement = progress_data.get("quiz_score"), progress_data.get("engagement_level")
                        needs_attention, reasons = False, []
                        if quiz_score is not None and isinstance(quiz_score, (int, float)) and quiz_score < 60: needs_attention, reasons = True, reasons + [f"Quiz score {quiz_score}% (<60%)"]
                        if isinstance(engagement, str) and engagement.lower() == "low": needs_attention, reasons = True, reasons + ["Low engagement reported"]
                        if needs_attention:
                            print(f"SCHEDULER: Alert for {student_name}, {course_name}, lesson {lesson_id_for_api}.")
                            subject = f"Student Progress Alert: {student_name} in {course_name}"; reasons_html = "".join([f"<li>{r}</li>" for r in reasons])
                            details_url = progress_data.get('details_url'); details_link_html = f"<p>Details: <a href='{details_url}'>View Progress</a></p>" if details_url else ""
                            body_html = f"""<html><body><p>Dear {instructor_name},</p><p>Alert for <strong>{student_name}</strong> in <strong>{course_name}</strong> (Lesson "{lesson.get('topic_summary', lesson_id_for_api)}", Date: {lesson_date.strftime('%B %d, %Y')}):</p><ul>{reasons_html}</ul>{details_link_html}<p>Please consider engaging with the student.</p><p>AI Tutor Monitoring</p></body></html>"""
                            send_email_notification(instructor_email, subject, body_html, instructor_name)
                    except Exception as e_prog: print(f"SCHEDULER: Error processing progress for {student_name}: {e_prog}")
        except Exception as e_course: print(f"SCHEDULER: Error in progress check for {config_file.name}: {e_course}")


# --- Gradio Callbacks ---
def _get_syllabus_text_from_config(course_name_str):
    if not course_name_str: return "Error: Course name not available."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try: cfg = json.loads(path.read_text(encoding="utf-8")); return generate_syllabus(cfg)
    except Exception as e: return f"Error loading syllabus: {e}"

def _get_plan_text_from_config(course_name_str):
    if not course_name_str: return "Error: Course name not available."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        plan_text = cfg.get("lesson_plan_formatted")
        return plan_text if plan_text else "Lesson plan not generated yet."
    except Exception as e: return f"Error loading lesson plan: {e}"

def enable_edit_syllabus_and_reload(current_course_name, current_output_content):
    if not current_output_content.strip().startswith("Course Name:"):
        syllabus_text = _get_syllabus_text_from_config(current_course_name)
        return gr.update(value=syllabus_text, interactive=True)
    return gr.update(interactive=True)

def enable_edit_plan_and_reload(current_course_name_for_plan, current_plan_output_content):
    if not current_plan_output_content.strip().startswith("**Week") and \
       (current_plan_output_content.strip().startswith("✅") or \
        current_plan_output_content.strip().startswith("⚠️")):
        plan_text = _get_plan_text_from_config(current_course_name_for_plan)
        return gr.update(value=plan_text, interactive=True)
    return gr.update(interactive=True)

def save_setup(course_name, instr_name, instr_email, devices, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected, students_input_str):
    num_expected_outputs = 11
    def error_return_tuple(error_message_str):
        return (gr.update(value=error_message_str, visible=True, interactive=False), gr.update(visible=True), None, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(value="", visible=False))
    try:
        if not all([course_name, instr_name, instr_email, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected]): return error_return_tuple("⚠️ Error: All fields marked with * are required.")
        try:
            start_dt, end_dt = datetime(int(sy), int(sm), int(sd_day)), datetime(int(ey), int(em), int(ed_day))
            if end_dt <= start_dt: return error_return_tuple("⚠️ Error: End date must be after start date.")
        except ValueError: return error_return_tuple("⚠️ Error: Invalid date selected.")

        sections_for_desc_obj = split_sections(pdf_file) # Pass the Gradio file object
        if not sections_for_desc_obj or (len(sections_for_desc_obj) == 1 and "Error" in sections_for_desc_obj[0]['title']):
             return error_return_tuple("⚠️ Error: Could not extract structural sections from PDF for analysis.")

        full_pdf_text, char_offset_to_page_map, current_char_offset = "", [], 0
        if fitz_available:
            doc_for_full_text = None
            try:
                if hasattr(pdf_file, "name"): doc_for_full_text = fitz.open(pdf_file.name)
                elif hasattr(pdf_file, "read"):
                    pdf_bytes = pdf_file.read(); pdf_file.seek(0)
                    doc_for_full_text = fitz.open(stream=pdf_bytes, filetype="pdf")
                if doc_for_full_text:
                    for page_num_fitz, page_obj in enumerate(doc_for_full_text):
                        page_text = page_obj.get_text()
                        if page_text: char_offset_to_page_map.append((current_char_offset, page_num_fitz + 1)); full_pdf_text += page_text + "\n"; current_char_offset += len(page_text) + 1
                    doc_for_full_text.close()
            except Exception as e_fitz_full: print(f"Error extracting full text with fitz: {e_fitz_full}"); fitz_available_for_full_text = False # Mark as failed
        if not full_pdf_text.strip() and sections_for_desc_obj : # Fallback if fitz failed for full text but PyPDF2 sections exist
            print("Warning: Fitz failed for full text extraction, using concatenated PyPDF2 sections. Page map will be empty.")
            full_pdf_text = "\n".join(s['content'] for s in sections_for_desc_obj)
            char_offset_to_page_map = [] # No reliable page map from PyPDF2 like this
        if not full_pdf_text.strip(): return error_return_tuple("⚠️ Error: Extracted PDF text is empty.")

        full_content_for_ai_desc = "\n\n".join(f"Title: {s['title']}\nSnippet: {s['content'][:1000]}" for s in sections_for_desc_obj)
        r1 = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"Generate a concise course description (2-3 sentences)."},{"role":"user","content": full_content_for_ai_desc}])
        desc = r1.choices[0].message.content.strip()
        r2 = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"Generate 5–10 clear, actionable learning objectives. Start each with a verb."},{"role":"user","content": full_content_for_ai_desc}])
        objs = [ln.strip(" -•*") for ln in r2.choices[0].message.content.splitlines() if ln.strip()]
        parsed_students = [{"id": str(uuid.uuid4()), "name": n.strip(), "email": e.strip()} for ln in students_input_str.splitlines() if ',' in ln for n, e in [ln.split(',', 1)]]
        cfg = {"course_name": course_name, "instructor": {"name": instr_name, "email": instr_email}, "class_days": class_days_selected, "start_date": f"{sy}-{sm}-{sd_day}", "end_date": f"{ey}-{em}-{ed_day}", "allowed_devices": devices, "students": parsed_students, "sections_for_description": sections_for_desc_obj, "full_text_content": full_pdf_text, "char_offset_page_map": char_offset_to_page_map, "course_description": desc, "learning_objectives": objs, "lessons": [], "lesson_plan_formatted": ""}
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        syllabus_text = generate_syllabus(cfg)
        return (gr.update(value=syllabus_text, visible=True, interactive=False), gr.update(visible=False), None, gr.update(visible=True), gr.update(visible=True), gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), gr.update(visible=True), gr.update(value="", visible=False))
    except openai.APIError as oai_err: print(f"OpenAI Error: {oai_err}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ OpenAI API Error: {oai_err}.")
    except Exception as e: print(f"Error in save_setup: {e}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ Error: {e}")

def generate_plan_callback(course_name_from_input):
    def error_return_for_plan(error_message_str):
        return (gr.update(value=error_message_str, visible=True, interactive=False), None, None, gr.update(visible=True), None, None, gr.update(visible=False), gr.update(visible=False))
    try:
        if not course_name_from_input: return error_return_for_plan("⚠️ Error: Course Name is required.")
        path = CONFIG_DIR / f"{course_name_from_input.replace(' ','_').lower()}_config.json"
        if not path.exists(): return error_return_for_plan(f"⚠️ Error: Config for '{course_name_from_input}' not found.")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg)
        cfg["lessons"], cfg["lesson_plan_formatted"] = structured_lessons_list, formatted_plan_str
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return (gr.update(value=formatted_plan_str, visible=True, interactive=False), None, None, gr.update(visible=False), None, None, gr.update(visible=True), gr.update(visible=True)) # Hide generate button on success
    except openai.APIError as oai_err: print(f"OpenAI Error: {oai_err}\n{traceback.format_exc()}"); return error_return_for_plan(f"⚠️ OpenAI API Error: {oai_err}.")
    except Exception as e: print(f"Error in generate_plan_callback: {e}\n{traceback.format_exc()}"); return error_return_for_plan(f"⚠️ Error: {e}")

def email_document_callback(course_name, doc_type, output_text_content, students_input_str):
    if not SMTP_USER or not SMTP_PASS: return gr.update(value="⚠️ Error: SMTP settings not configured.")
    try:
        if not course_name or not output_text_content: return gr.update(value=f"⚠️ Error: Course Name & {doc_type} content required.")
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.")
        cfg = json.loads(path.read_text(encoding="utf-8")); instr_name, instr_email = cfg.get("instructor", {}).get("name", "Instructor"), cfg.get("instructor", {}).get("email")
        buf, fn = download_docx(output_text_content, f"{course_name.replace(' ','_')}_{doc_type.lower()}.docx"); attachment_data = buf.read()
        recipients = ([{"name": instr_name, "email": instr_email}] if instr_email else []) + [{"name": n.strip(), "email": e.strip()} for ln in students_input_str.splitlines() if ',' in ln for n, e in [ln.split(',', 1)]]
        if not recipients: return gr.update(value=f"⚠️ Error: No recipients found.")
        success_count, errors = 0, []
        for rec in recipients:
            msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = f"Course {doc_type.capitalize()}: {course_name}", SMTP_USER, rec["email"]
            msg.set_content(f"Hi {rec['name']},\n\nAttached is {doc_type.lower()} for {course_name}.\n\nBest,\nAI Tutor System"); msg.add_attachment(attachment_data, maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=fn)
            try:
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                    s.starttls()  # Start TLS encryption
                    s.login(SMTP_USER, SMTP_PASS) # Login to the server
                    s.send_message(msg) # Send the email message
                # Increment success_count *after* the 'with' block if successful
                success_count += 1 
            except Exception as e_smtp:
                error_msg = f"SMTP Error sending to {rec['email']}: {e_smtp}"
                print(error_msg)
                # Optionally include traceback for more detail in logs
                # print(traceback.format_exc()) 
                errors.append(error_msg)
        status_message = f"✅ {doc_type.capitalize()} emailed to {success_count} recipient(s)."; status_message += f"\n⚠️ Errors:\n" + "\n".join(errors) if errors else ""
        return gr.update(value=status_message)
    except Exception as e: error_text = f"⚠️ Error emailing {doc_type.lower()}:\n{traceback.format_exc()}"; print(error_text); return gr.update(value=error_text)

def email_syllabus_callback(course_name, students_input_str, output_box_content): return email_document_callback(course_name, "Syllabus", output_box_content, students_input_str)
def email_plan_callback(course_name, students_input_str, output_box_content): return email_document_callback(course_name, "Lesson Plan", output_box_content, students_input_str)

# --- Build UI ---
def build_ui():
    with gr.Blocks(theme=gr.themes.Soft()) as demo:
        gr.Markdown("## AI Tutor Instructor Panel")
        with gr.Tabs():
            with gr.TabItem("Course Setup & Syllabus"):
                with gr.Row(): course, instr, email = gr.Textbox(label="Course Name*"), gr.Textbox(label="Instructor Name*"), gr.Textbox(label="Instructor Email*", type="email")
                pdf_file = gr.File(label="Upload Course Material PDF*", file_types=[".pdf"])
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("#### Course Schedule"); years, months, days_list = [str(y) for y in range(datetime.now().year, datetime.now().year + 5)], [f"{m:02d}" for m in range(1,13)], [f"{d:02d}" for d in range(1,32)]
                        with gr.Row(): sy, sm, sd_day = gr.Dropdown(years, label="Start Year*"), gr.Dropdown(months, label="Start Month*"), gr.Dropdown(days_list, label="Start Day*")
                        with gr.Row(): ey, em, ed_day = gr.Dropdown(years, label="End Year*"), gr.Dropdown(months, label="End Month*"), gr.Dropdown(days_list, label="End Day*")
                        class_days_selected = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*")
                    with gr.Column(scale=1): gr.Markdown("#### Student & Access"); devices = gr.CheckboxGroup(["Phone","PC", "Tablet"], label="Allowed Devices", value=["PC"]); students_input_str = gr.Textbox(label="Students (Name,Email per line)", lines=5, placeholder="S. One,s1@ex.com\nS. Two,s2@ex.com")
                btn_save = gr.Button("1. Save Setup & Generate Syllabus", variant="primary"); gr.Markdown("---")
                output_box = gr.Textbox(label="Output", lines=20, interactive=False, visible=False, show_copy_button=True) 
                with gr.Row(visible=False) as syllabus_actions_row: btn_edit_syl, btn_email_syl = gr.Button(value="📝 Edit Syllabus Text"), gr.Button(value="📧 Email Syllabus", variant="secondary")
            with gr.TabItem("Lesson Plan Management"):
                gr.Markdown("Enter course name (auto-filled from Tab 1), then generate plan.")
                course_load_for_plan = gr.Textbox(label="Course Name for Lesson Plan", placeholder="e.g., Introduction to Python")
                output_plan_box = gr.Textbox(label="Lesson Plan Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                with gr.Row(visible=False) as plan_buttons_row: btn_generate_plan, btn_edit_plan, btn_email_plan = gr.Button("2. Generate/Re-generate Lesson Plan", variant="primary"), gr.Button(value="📝 Edit Plan Text"), gr.Button(value="📧 Email Lesson Plan", variant="secondary")
            with gr.TabItem("Contact Support"):
                gr.Markdown("### Send a Message to Support")
                with gr.Row(): contact_name, contact_email_addr = gr.Textbox(label="Your Name"), gr.Textbox(label="Your Email Address")
                contact_message = gr.Textbox(label="Message", lines=5, placeholder="Type your message here..."); btn_send_contact_email = gr.Button("Send Message", variant="primary"); contact_status_output = gr.Markdown(value="")
        
        dummy_btn_1, dummy_btn_2, dummy_btn_3, dummy_btn_4 = gr.Button(visible=False), gr.Button(visible=False), gr.Button(visible=False), gr.Button(visible=False)
        btn_save.click(save_setup, inputs=[course,instr,email,devices,pdf_file,sy,sm,sd_day,ey,em,ed_day,class_days_selected,students_input_str], outputs=[output_box, btn_save, dummy_btn_1, btn_generate_plan, btn_edit_syl, btn_email_syl, btn_edit_plan, btn_email_plan, syllabus_actions_row, plan_buttons_row, output_plan_box])
        btn_edit_syl.click(enable_edit_syllabus_and_reload, inputs=[course, output_box], outputs=[output_box])
        btn_email_syl.click(email_syllabus_callback, inputs=[course, students_input_str, output_box], outputs=[output_box])
        btn_generate_plan.click(generate_plan_callback, inputs=[course_load_for_plan], outputs=[output_plan_box, dummy_btn_2, dummy_btn_1, btn_generate_plan, dummy_btn_3, dummy_btn_4, btn_edit_plan, btn_email_plan]).then(lambda: (gr.update(visible=True), gr.update(visible=True)), outputs=[output_plan_box, plan_buttons_row])
        btn_edit_plan.click(enable_edit_plan_and_reload, inputs=[course_load_for_plan, output_plan_box], outputs=[output_plan_box])
        btn_email_plan.click(email_plan_callback, inputs=[course_load_for_plan, students_input_str, output_plan_box], outputs=[output_plan_box])
        course.change(lambda x: x, inputs=[course], outputs=[course_load_for_plan])
        def handle_contact_submission(name, email_addr, message):
            if not all([name, email_addr, message]): return gr.update(value="<p style='color:red;'>Error: Name, Email, and Message are required.</p>")
            if "@" not in email_addr or "." not in email_addr: return gr.update(value="<p style='color:red;'>Error: Please enter a valid email address.</p>")
            subject, support_recipient_email = f"AI Tutor Panel Contact: {name}", "easyaitutor@gmail.com" 
            html_body = f"<html><body><h3>New Contact Request</h3><p><strong>Name:</strong> {name}</p><p><strong>Email:</strong> {email_addr}</p><hr><p><strong>Message:</strong></p><p>{message.replace(chr(10), '<br>')}</p></body></html>"
            success = send_email_notification(support_recipient_email, subject, html_body, name)
            if success: return (gr.update(value="<p style='color:green;'>Message sent successfully!</p>"), gr.update(value=""), gr.update(value=""), gr.update(value=""))
            else: return gr.update(value="<p style='color:red;'>Error: Could not send message. Try again later.</p>")
        btn_send_contact_email.click(handle_contact_submission, inputs=[contact_name, contact_email_addr, contact_message], outputs=[contact_status_output, contact_name, contact_email_addr, contact_message])
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
    print("Starting Gradio UI locally. For production, use Uvicorn.")
    gradio_app_instance.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT",7860)))
    try:
        while True: time.sleep(2) # Keep main thread alive for scheduler in direct run
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down scheduler...")
        if scheduler.running: scheduler.shutdown()
