from dotenv import load_dotenv
load_dotenv()
import os
import io
import json
import traceback
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone as dt_timezone
import uuid
import random
import mimetypes
import csv

import openai
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
# from fastapi.templating import Jinja2Templates # Not used
# from fastapi.staticfiles import StaticFiles # Not used

import jwt
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.middleware.cors import CORSMiddleware

# --- Configuration ---
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)
PROGRESS_LOG_FILE = CONFIG_DIR / "student_progress_log.csv"

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SUPPORT_EMAIL_ADDRESS = os.getenv("SUPPORT_EMAIL_ADDRESS", "easyaitutor@gmail.com") # For contact form

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "a-very-secure-secret-key-please-change")
if JWT_SECRET_KEY == "a-very-secure-secret-key-please-change":
    print("WARNING: JWT_SECRET_KEY is set to default. Please set a strong, unique secret key in your environment variables.")
LINK_VALIDITY_HOURS = 6
ALGORITHM = "HS256"
APP_DOMAIN = os.getenv("APP_DOMAIN", "https://www.easyaitutor.com")

days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

STUDENT_TTS_MODEL = "tts-1"
STUDENT_CHAT_MODEL = "gpt-4o-mini"
STUDENT_WHISPER_MODEL = "whisper-1"
STUDENT_DEFAULT_ENGLISH_LEVEL = "B1 (Intermediate)"
STUDENT_AUDIO_DIR = Path("student_audio_files")
STUDENT_AUDIO_DIR.mkdir(exist_ok=True)
STUDENT_BOT_NAME = "Easy AI Tutor"
STUDENT_ONBOARDING_TURNS = 2
STUDENT_TEACHING_TURNS_PER_BREAK = 5
STUDENT_INTEREST_BREAK_TURNS = 1
STUDENT_QUIZ_AFTER_TURNS = 7
STUDENT_MAX_SESSION_TURNS = 20
STUDENT_UI_PATH = "/student_tutor_interface"

try:
    import fitz
    fitz_available = True
except ImportError:
    fitz_available = False
    print("PyMuPDF (fitz) not found. PDF processing quality may be reduced. Page number mapping will be limited.")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)
scheduler = BackgroundScheduler(timezone="UTC")

@app.get("/healthz")
def healthz():
    return {"status": "ok", "scheduler_running": scheduler.running, "jobs": [job.id for job in scheduler.get_jobs()]}

# --- PDF Processing & Helpers ---
def split_sections(pdf_file_obj): # Expects an opened file-like object
    if not hasattr(pdf_file_obj, "read") or not hasattr(pdf_file_obj, "seek"):
        print("Error (split_sections): input is not a valid file-like object.")
        return [{'title': 'Input Error', 'content': 'Invalid PDF file object provided.', 'page': None}]

    pdf_file_obj.seek(0)
    if fitz_available:
        try:
            # fitz.open can take a stream
            doc = fitz.open(stream=pdf_file_obj.read(), filetype="pdf")
            pdf_file_obj.seek(0) # Reset pointer if read
            
            pages_text = [page.get_text("text", sort=True) for page in doc]
            doc.close()
            
            headings = []
            for i, text_content in enumerate(pages_text):
                for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad|Part|Module)\s+[\d\w]+.*", text_content):
                    headings.append({"page": i + 1, "start_char_index_on_page": m.start(), "title": m.group().strip(), "page_index": i})
            headings.sort(key=lambda h: (h['page_index'], h['start_char_index_on_page']))
            
            sections = []
            if not headings:
                full_content = "\n".join(pages_text)
                if full_content.strip(): sections.append({'title': 'Full Document Content', 'content': full_content.strip(), 'page': 1})
                return sections

            for idx, h_info in enumerate(headings):
                current_page_idx, start_char_on_page = h_info['page_index'], h_info['start_char_index_on_page']
                content_buffer = ''
                if idx + 1 < len(headings):
                    next_h_info, next_page_idx, end_char_on_page_of_next = headings[idx+1], headings[idx+1]['page_index'], headings[idx+1]['start_char_index_on_page']
                    if current_page_idx == next_page_idx: content_buffer += pages_text[current_page_idx][start_char_on_page:end_char_on_page_of_next]
                    else:
                        content_buffer += pages_text[current_page_idx][start_char_on_page:] + '\n'
                        for p_idx in range(current_page_idx + 1, next_page_idx): content_buffer += pages_text[p_idx] + '\n'
                        content_buffer += pages_text[next_page_idx][:end_char_on_page_of_next]
                else:
                    content_buffer += pages_text[current_page_idx][start_char_on_page:] + '\n'
                    for p_idx in range(current_page_idx + 1, len(pages_text)): content_buffer += pages_text[p_idx] + '\n'
                if content_buffer.strip(): sections.append({'title': h_info['title'], 'content': content_buffer.strip(), 'page': h_info['page']})
            
            sections = [s for s in sections if len(s['content']) > max(50, len(s['title']) + 20)] 
            if not sections and "".join(pages_text).strip():
                 sections.append({'title': 'Full Document (Fallback)', 'content': "".join(pages_text).strip(), 'page': 1})
            return sections
        except Exception as e_fitz:
            print(f"Error processing PDF with fitz: {e_fitz}. Attempting PyPDF2 fallback.")
            pdf_file_obj.seek(0) # Reset for PyPDF2
    try:
        from PyPDF2 import PdfReader
        pdf_file_obj.seek(0)
        reader = PdfReader(pdf_file_obj)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip(): return [{'title': 'PDF Error (PyPDF2)', 'content': 'No text extracted.', 'page': None}]
        chunks, sections, sents_per_sec = re.split(r'(?<=[.?!])\s+', text), [], 15
        for i in range(0, len(chunks), sents_per_sec):
            content = " ".join(chunks[i:i+sents_per_sec]).strip()
            if content: sections.append({'title': f'Content Block {i//sents_per_sec + 1}', 'content': content, 'page': None})
        if not sections and text.strip(): sections.append({'title': 'Full Document (PyPDF2)', 'content': text.strip(), 'page': None})
        return sections
    except ImportError: return [{'title': 'PDF Library Error', 'content': 'PyPDF2 not available.', 'page': None}]
    except Exception as e_pypdf2: return [{'title': 'PDF Error (PyPDF2)', 'content': f'{e_pypdf2}', 'page': None}]

def download_docx(content, filename):
    # (Keep existing logic)
    buf = io.BytesIO(); doc = Document()
    for line in content.split("\n"):
        p = doc.add_paragraph()
        parts = re.split(r'(\*\*.*?\*\*)', line)
        for part in parts:
            if part.startswith('**') and part.endswith('**'): p.add_run(part[2:-2]).bold = True
            else: p.add_run(part)
    doc.save(buf); buf.seek(0); return buf, filename

def count_classes(start_date_obj, end_date_obj, weekday_indices):
    # (Keep existing logic)
    cnt, cur = 0, start_date_obj
    while cur <= end_date_obj:
        if cur.weekday() in weekday_indices: cnt += 1
        cur += timedelta(days=1)
    return cnt
    
def generate_access_token(student_id, course_id, lesson_id, lesson_date_obj):
    # (Keep existing logic)
    if isinstance(lesson_date_obj, str): lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()
    iat = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    exp = iat + timedelta(hours=LINK_VALIDITY_HOURS)
    payload = {"sub": str(student_id), "course_id": str(course_id), "lesson_id": int(lesson_id), "iat": iat, "exp": exp, "aud": APP_DOMAIN}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)

def generate_5_digit_code(): return str(random.randint(10000, 99999))

def send_email_notification(to_email, subject, html_content, from_name="AI Tutor System", 
                            attachment_filepath_str=None, # MODIFIED: Expect filepath string
                            attachment_filename_override=None):
    if not SMTP_USER or not SMTP_PASS:
        print(f"CRITICAL SMTP ERROR: SMTP_USER or SMTP_PASS not configured. Cannot send email to {to_email}.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{SMTP_USER}>" if "@" in SMTP_USER else f"{from_name} <default_sender@example.com>"
    msg["To"] = to_email
    if "@" in from_name and from_name.lower() != SMTP_USER.lower(): msg.add_header('Reply-To', from_name)
    msg.add_alternative(html_content, subtype='html')

    if attachment_filepath_str: # MODIFIED: Use filepath string
        filename_to_use = attachment_filename_override or os.path.basename(attachment_filepath_str)
        try:
            with open(attachment_filepath_str, 'rb') as fp: # MODIFIED: Open the file from path
                file_data = fp.read()
            
            ctype, encoding = mimetypes.guess_type(filename_to_use)
            if ctype is None or encoding is not None: ctype = 'application/octet-stream'
            maintype, subtype_val = ctype.split('/', 1)
            
            msg.add_attachment(file_data, maintype=maintype, subtype=subtype_val, filename=filename_to_use)
            print(f"Attachment '{filename_to_use}' prepared for email to {to_email}.")
        except FileNotFoundError:
            print(f"Error attaching: File not found at {attachment_filepath_str}")
        except Exception as e_attach:
            print(f"Error processing attachment '{filename_to_use}' for email to {to_email}: {e_attach}")
    # (Rest of SMTP logic remains the same)
    try:
        print(f"Attempting to send email titled '{subject}' to {to_email} via {SMTP_SERVER}:{SMTP_PORT} as {SMTP_USER}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
            s.set_debuglevel(0) 
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print(f"Email successfully sent to {to_email}"); return True
    except smtplib.SMTPAuthenticationError as e: print(f"SMTP Auth Error for {SMTP_USER}: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPConnectError as e: print(f"SMTP Connect Error to {SMTP_SERVER}:{SMTP_PORT}: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPServerDisconnected as e: print(f"SMTP Server Disconnected: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPRecipientsRefused as e: print(f"SMTP Recipient Refused for {to_email}: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPException as e: print(f"General SMTP Exception to {to_email}: {e}\n{traceback.format_exc()}"); return False
    except Exception as e: print(f"Unexpected error sending email to {to_email}: {e}\n{traceback.format_exc()}"); return False


# --- Syllabus & Lesson Plan Generation (Instructor Panel) ---
# (generate_syllabus, generate_plan_by_week_structured_and_formatted - keep existing logic)
# (send_daily_class_reminders, log_student_progress, check_student_progress_and_notify_professor - keep existing logic)
# (_get_syllabus_text_from_config, _get_plan_text_from_config, enable_edit_syllabus_and_reload, enable_edit_plan_and_reload - keep existing logic)
# (generate_plan_callback, email_document_callback, email_syllabus_callback, email_plan_callback - keep existing logic, but email_document_callback will use the modified send_email_notification)

def generate_syllabus(cfg): # (No changes needed here from your last version)
    sd_str, ed_str = cfg.get('start_date'), cfg.get('end_date')
    if not sd_str or not ed_str: return "Error: Start or end date missing in config."
    try: sd, ed = datetime.strptime(sd_str, '%Y-%m-%d').date(), datetime.strptime(ed_str, '%Y-%m-%d').date()
    except ValueError: return "Error: Invalid date format in config."
    month_range_str = f"{sd.strftime('%B %Y')} – {ed.strftime('%B %Y')}"
    if sd.year == ed.year: month_range_str = f"{sd.strftime('%B')} – {ed.strftime('%B %Y')}" if sd.month != ed.month else f"{sd.strftime('%B %Y')}"
    total_classes_val = count_classes(sd, ed, [days_map[d] for d in cfg.get('class_days', [])])
    header = [f"**Course:** {cfg.get('course_name', 'N/A')}", f"**Instructor:** {cfg.get('instructor', {}).get('name', 'N/A')}", f"**Email:** {cfg.get('instructor', {}).get('email', 'N/A')}", f"**Duration:** {month_range_str} ({total_classes_val} classes)", '-'*60]
    objectives_list = [f" • {obj}" for obj in cfg.get('learning_objectives', [])]
    body_content = ["**COURSE DESCRIPTION:**", cfg.get('course_description', 'Not specified.'), "", "**LEARNING OBJECTIVES:**"] + objectives_list + ["", "**GRADING POLICY:**", " • Participation in AI Tutor sessions.", " • Completion of per-lesson quizzes (retake if score < 60%).", " • Final understanding assessed based on overall engagement and quiz performance.", "", "**CLASS SCHEDULE:**", f" • Classes typically run on: {', '.join(cfg.get('class_days', ['N/A']))}", f" • Check daily email reminders for exact lesson access links.", "", "**SUPPORT & OFFICE HOURS:**", " • For technical issues with the AI Tutor, use the 'Contact Support' tab in the Instructor Panel.", " • For course content questions, please reach out to your instructor directly."]
    return "\n".join(header + [""] + body_content)

def generate_plan_by_week_structured_and_formatted(cfg): # (No changes needed here from your last version)
    sd_str, ed_str = cfg.get('start_date'), cfg.get('end_date')
    if not sd_str or not ed_str: return "Error: Start or end date missing.", []
    try: sd, ed = datetime.strptime(sd_str, '%Y-%m-%d').date(), datetime.strptime(ed_str, '%Y-%m-%d').date()
    except ValueError: return "Error: Invalid date format.", []
    selected_weekdays = {days_map[d] for d in cfg.get('class_days', [])}
    if not selected_weekdays: return "Error: No class days selected.", []
    class_dates_list = [current_date for i in range((ed - sd).days + 1) if (current_date := sd + timedelta(days=i)).weekday() in selected_weekdays]
    if not class_dates_list: return "No class dates fall within the specified range and selected weekdays.", []
    full_text_content, char_offset_map = cfg.get("full_text_content", ""), cfg.get("char_offset_page_map", [])
    if not full_text_content.strip():
        placeholder_lessons, placeholder_lines, lessons_by_course_week_dict = [], [], {}
        course_week_counter_ph, current_week_monday_for_grouping_ph = 0, None
        for idx, dt_obj in enumerate(class_dates_list):
            monday_of_this_week_ph = dt_obj - timedelta(days=dt_obj.weekday())
            if current_week_monday_for_grouping_ph is None or monday_of_this_week_ph > current_week_monday_for_grouping_ph: course_week_counter_ph += 1; current_week_monday_for_grouping_ph = monday_of_this_week_ph
            year_of_this_course_week_ph, course_week_key_ph = monday_of_this_week_ph.year, f"{year_of_this_course_week_ph}-CW{course_week_counter_ph:02d}"
            lesson_entry = {"lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'), "topic_summary": "Topic TBD (No PDF text provided)", "original_section_title": "N/A", "page_reference": None}
            placeholder_lessons.append(lesson_entry); lessons_by_course_week_dict.setdefault(course_week_key_ph, []).append(lesson_entry)
        for course_wk_key_ph_sorted in sorted(lessons_by_course_week_dict.keys()):
            year_disp_ph, course_week_num_disp_str_ph = course_wk_key_ph_sorted.split("-CW"); first_date_in_group_ph = lessons_by_course_week_dict[course_wk_key_ph_sorted][0]['date']; year_of_first_date_ph = datetime.strptime(first_date_in_group_ph, '%Y-%m-%d').year
            placeholder_lines.append(f"**Course Week {int(course_week_num_disp_str_ph)} (Year {year_of_first_date_ph})**\n")
            for lsn_item in lessons_by_course_week_dict[course_wk_key_ph_sorted]: placeholder_lines.append(f"**Lesson {lsn_item['lesson_number']} ({datetime.strptime(lsn_item['date'], '%Y-%m-%d').strftime('%B %d, %Y')})**: {lsn_item['topic_summary']}")
            placeholder_lines.append('')
        return "\n".join(placeholder_lines), placeholder_lessons
    total_chars_in_text, num_lessons_to_plan = len(full_text_content), len(class_dates_list)
    chars_per_lesson_segment = total_chars_in_text // num_lessons_to_plan if num_lessons_to_plan > 0 else total_chars_in_text
    min_chars_for_summary, lesson_topic_summaries, current_char_pointer, segment_start_chars = 150, [], 0, []
    client = openai.OpenAI()
    for i in range(num_lessons_to_plan):
        segment_start_chars.append(current_char_pointer); start_index, end_index = current_char_pointer, current_char_pointer + chars_per_lesson_segment if i < num_lessons_to_plan - 1 else total_chars_in_text
        text_segment_for_summary, current_char_pointer = full_text_content[start_index:end_index].strip(), end_index
        if len(text_segment_for_summary) < min_chars_for_summary: lesson_topic_summaries.append("Review of previous topics or brief discussion.")
        else:
            try:
                response = client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "system", "content": "You are a helpful assistant. Identify the core concept from the provided text. Respond ONLY with a short, concise phrase (maximum 10-12 words, ideally a gerund phrase like 'Using verbs effectively') suitable as a lesson topic title. Do NOT use full sentences or any introductory/concluding remarks."}, {"role": "user", "content": text_segment_for_summary}], temperature=0.4, max_tokens=30)
                summary = response.choices[0].message.content.strip().replace('"', '').replace('.', '').capitalize(); lesson_topic_summaries.append(summary if summary else f"Content Analysis Segment {i+1}")
            except Exception as e: print(f"Error summarizing segment {i+1} with OpenAI: {e}"); lesson_topic_summaries.append(f"Topic for segment {i+1} (AI summary error)")
    lessons_by_course_week_dict, structured_lessons_list, course_week_counter, current_week_monday_for_grouping = {}, [], 0, None
    for idx, dt_obj in enumerate(class_dates_list):
        monday_of_this_week = dt_obj - timedelta(days=dt_obj.weekday())
        if current_week_monday_for_grouping is None or monday_of_this_week > current_week_monday_for_grouping: course_week_counter += 1; current_week_monday_for_grouping = monday_of_this_week
        year_of_this_course_week, course_week_key = monday_of_this_week.year, f"{year_of_this_course_week}-CW{course_week_counter:02d}"
        summary_for_this_lesson, estimated_page_ref = lesson_topic_summaries[idx] if idx < len(lesson_topic_summaries) else "Topic TBD", None
        if char_offset_map:
            seg_start_char_offset = segment_start_chars[idx]
            for offset, page_num in reversed(char_offset_map): 
                if seg_start_char_offset >= offset: estimated_page_ref = page_num; break
            if estimated_page_ref is None and char_offset_map: estimated_page_ref = char_offset_map[0][1]
        lesson_entry = {"lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'), "topic_summary": summary_for_this_lesson, "original_section_title": f"Text Segment {idx+1}", "page_reference": estimated_page_ref}
        structured_lessons_list.append(lesson_entry); lessons_by_course_week_dict.setdefault(course_week_key, []).append(lesson_entry)
    formatted_plan_lines = []
    for course_wk_key_sorted in sorted(lessons_by_course_week_dict.keys()):
        first_lesson_in_group, first_date_obj_in_group = lessons_by_course_week_dict[course_wk_key_sorted][0], datetime.strptime(lessons_by_course_week_dict[course_wk_key_sorted][0]['date'], '%Y-%m-%d')
        course_week_num_from_key = int(course_wk_key_sorted.split("-CW")[1])
        formatted_plan_lines.append(f"**Course Week {course_week_num_from_key} (Year {first_date_obj_in_group.year})**\n")
        for lesson_item in lessons_by_course_week_dict[course_wk_key_sorted]:
            date_str_formatted, page_ref_str = datetime.strptime(lesson_item['date'], '%Y-%m-%d').strftime('%B %d, %Y'), f" (Approx. Ref. p. {lesson_item['page_reference']})" if lesson_item['page_reference'] else ''
            formatted_plan_lines.append(f"**Lesson {lesson_item['lesson_number']} ({date_str_formatted})**{page_ref_str}: {lesson_item['topic_summary']}")
        formatted_plan_lines.append('')
    return "\n".join(formatted_plan_lines), structured_lessons_list

def send_daily_class_reminders(): # (No changes needed here from your last version)
    now_utc, today_utc_date = datetime.now(dt_timezone.utc), datetime.now(dt_timezone.utc).date()
    print(f"SCHEDULER: Running daily class reminder job at {now_utc.isoformat()} for date {today_utc_date}")
    course_configs_found, reminders_sent_total = 0, 0
    for config_file in CONFIG_DIR.glob("*_config.json"):
        course_configs_found += 1; course_id_from_filename = config_file.stem.replace("_config", "")
        print(f"SCHEDULER: Processing course config: {config_file.name} (ID: {course_id_from_filename})")
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8")); course_name, lessons, students = cfg.get("course_name", course_id_from_filename), cfg.get("lessons"), cfg.get("students")
            if not lessons: print(f"SCHEDULER: No lessons in config for '{course_name}'. Skipping."); continue
            if not students: print(f"SCHEDULER: No students in config for '{course_name}'. Skipping."); continue
            print(f"SCHEDULER: Found {len(lessons)} lessons, {len(students)} students for '{course_name}'.")
            for lesson in lessons:
                lesson_number, lesson_date_str, lesson_topic = lesson.get("lesson_number", "N/A"), lesson.get("date"), lesson.get('topic_summary', 'Lesson Topic')
                if not lesson_date_str: print(f"SCHEDULER: Lesson {lesson_number} in '{course_name}' missing date. Skipping."); continue
                try: lesson_date_obj = datetime.strptime(lesson_date_str, '%Y-%m-%d').date()
                except ValueError: print(f"SCHEDULER: Invalid date '{lesson_date_str}' for lesson {lesson_number} in '{course_name}'. Skipping."); continue
                if lesson_date_obj == today_utc_date:
                    print(f"SCHEDULER: MATCH! Class for '{course_name}' today: Lesson {lesson_number} - {lesson_topic}")
                    class_code, reminders_sent_for_this_class = generate_5_digit_code(), 0
                    for student in students:
                        student_id, student_email, student_name = student.get("id", f"unknown_{uuid.uuid4()}"), student.get("email"), student.get("name", "Student")
                        if not student_email: print(f"SCHEDULER: Student '{student_name}' ({student_id}) in '{course_name}' missing email. Skipping."); continue
                        try:
                            token = generate_access_token(student_id, course_id_from_filename, lesson_number, lesson_date_obj); access_link = f"{APP_DOMAIN}/class?token={token}"
                            email_subject = f"Today's AI Tutor Lesson for {course_name}: {lesson_topic}"
                            email_html_body = f"""<html><head><style>body {{font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4;}} .container {{max-width: 600px; margin: 20px auto; background-color: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1);}} h2 {{color: #333333;}} p {{color: #555555; line-height: 1.6;}} .button {{display: inline-block; background-color: #007bff; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold;}} .code {{font-size: 1.2em; font-weight: bold; color: #28a745; background-color: #e9ecef; padding: 3px 8px; border-radius: 4px;}} .footer {{font-size: 0.9em; color: #777777; margin-top: 20px; text-align: center;}}</style></head><body><div class="container"><h2>Your AI Tutor Lesson is Ready!</h2><p>Hi {student_name},</p><p>Your AI Tutor session for <strong>{course_name}</strong> on the topic "<strong>{lesson_topic}</strong>" is scheduled for today.</p><p><a href="{access_link}" class="button">Access Your Lesson</a></p><p>If the button doesn't work, copy and paste this link into your browser:<br><a href="{access_link}">{access_link}</a></p><p>Your reference code for today's session is: <span class="code">{class_code}</span> (You usually won't need this code if you use the link).</p><p>This link is valid from <strong>6:00 AM to 12:00 PM UTC</strong> on <strong>{lesson_date_obj.strftime('%B %d, %Y')}</strong>.</p><p>Happy learning!</p><div class="footer">The AI Tutor System</div></div></body></html>"""
                            print(f"SCHEDULER: Attempting to send reminder to {student_name} <{student_email}> for lesson {lesson_number} of '{course_name}'.")
                            if send_email_notification(student_email, email_subject, email_html_body, from_name=f"{STUDENT_BOT_NAME} ({course_name})"): reminders_sent_total += 1; reminders_sent_for_this_class += 1
                            else: print(f"SCHEDULER: Failed to send reminder to {student_name} <{student_email}> for lesson {lesson_number}.")
                        except Exception as e_token_email: print(f"SCHEDULER: Error for student {student_id} in '{course_name}': {e_token_email}\n{traceback.format_exc()}")
                    print(f"SCHEDULER: Sent {reminders_sent_for_this_class} reminders for Lesson {lesson_number} of '{course_name}'.")
        except FileNotFoundError: print(f"SCHEDULER: Config {config_file.name} not found. Skipping.")
        except json.JSONDecodeError: print(f"SCHEDULER: Error decoding JSON from {config_file.name}. Skipping.")
        except Exception as e: print(f"SCHEDULER: General error processing config {config_file.name}: {e}\n{traceback.format_exc()}")
    if course_configs_found == 0: print(f"SCHEDULER: No course configuration files found in '{CONFIG_DIR}'.")
    print(f"SCHEDULER: Daily class reminder job finished. Total reminders sent: {reminders_sent_total}")

def log_student_progress(student_id, course_id, lesson_id, quiz_score_str, session_duration_secs, engagement_notes="N/A"): # (No changes needed)
    if not PROGRESS_LOG_FILE.exists():
        with open(PROGRESS_LOG_FILE, 'w', newline='', encoding='utf-8') as f: csv.writer(f).writerow(["timestamp_utc", "student_id", "course_id", "lesson_id", "quiz_score", "session_duration_seconds", "engagement_notes"])
    with open(PROGRESS_LOG_FILE, 'a', newline='', encoding='utf-8') as f: csv.writer(f).writerow([datetime.now(dt_timezone.utc).isoformat(), student_id, course_id, lesson_id, quiz_score_str, round(session_duration_secs, 2), engagement_notes])
    print(f"Progress logged: Student {student_id}, Course {course_id}, Lesson {lesson_id}, Score {quiz_score_str}, Duration {session_duration_secs:.0f}s.")

def check_student_progress_and_notify_professor(): # (No changes needed)
    print(f"SCHEDULER: Running student progress check at {datetime.now(dt_timezone.utc).isoformat()}")
    if not PROGRESS_LOG_FILE.exists(): print("SCHEDULER (Progress Check): Progress log file does not exist. Skipping check."); return
    one_day_ago_utc, alerts_by_course_instructor = datetime.now(dt_timezone.utc) - timedelta(days=1), {}
    try:
        with open(PROGRESS_LOG_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader):
                try:
                    log_timestamp_utc = datetime.fromisoformat(row["timestamp_utc"])
                    if log_timestamp_utc < one_day_ago_utc: continue
                    quiz_score_str = row.get("quiz_score", "0/0")
                    if "/" in quiz_score_str:
                        parts = quiz_score_str.split('/');
                        if len(parts) == 2:
                            correct, total_qs = map(int, parts)
                            if total_qs > 0 and (correct / total_qs) < 0.60:
                                student_id, course_id, lesson_id, duration, notes = row["student_id"], row["course_id"], row["lesson_id"], row.get('session_duration_seconds','N/A'), row.get('engagement_notes','N/A')
                                config_path = CONFIG_DIR / f"{course_id.replace(' ','_').lower()}_config.json"
                                if not config_path.exists(): print(f"SCHEDULER (Progress Check): Config for course {course_id} not found."); continue
                                cfg = json.loads(config_path.read_text(encoding="utf-8")); instructor_info, instructor_email, instructor_name, course_name_from_cfg = cfg.get("instructor", {}), cfg.get("instructor", {}).get("email"), cfg.get("instructor", {}).get("name", "Instructor"), cfg.get("course_name", course_id)
                                if instructor_email:
                                    alert_msg = f"Student <strong>{student_id}</strong> scored {quiz_score_str} on lesson {lesson_id} (Session: {duration}s, Logged: {log_timestamp_utc.strftime('%Y-%m-%d %H:%M')} UTC). Notes: <em>{notes}</em>"
                                    instructor_alerts = alerts_by_course_instructor.setdefault(instructor_email, {}); course_alerts = instructor_alerts.setdefault(course_name_from_cfg, {"name": instructor_name, "alerts": []}); course_alerts["alerts"].append(alert_msg)
                        else: print(f"SCHEDULER (Progress Check): Malformed quiz_score '{quiz_score_str}' in log row {row_num+1}. Skipping.")
                except ValueError as ve: print(f"SCHEDULER (Progress Check): Skipping malformed row {row_num+1} in progress log: {ve} - Row: {row}")
                except Exception as e_row: print(f"SCHEDULER (Progress Check): Error processing row {row_num+1}: {e_row} - Row: {row}")
    except Exception as e_read_log: print(f"SCHEDULER (Progress Check): Error reading progress log '{PROGRESS_LOG_FILE}': {e_read_log}"); return
    for instructor_email, courses_data in alerts_by_course_instructor.items():
        full_alert_html_body = f"<html><body style='font-family: sans-serif;'><p>Dear {courses_data.get(next(iter(courses_data)), {}).get('name', 'Instructor')},</p><p>One or more students may require attention based on recent AI Tutor sessions:</p>"
        any_alerts_for_this_instructor = False
        for course_name, data in courses_data.items():
            if data["alerts"]: any_alerts_for_this_instructor = True; full_alert_html_body += f"<h3>Course: {course_name}</h3><ul>" + "".join([f"<li>{alert}</li>" for alert in data["alerts"]]) + "</ul>"
        if any_alerts_for_this_instructor:
            full_alert_html_body += f"<p>Please consider reviewing their progress and engaging with them directly.</p><p>Best regards,<br>AI Tutor Monitoring System</p></body></html>"
            send_email_notification(instructor_email, "AI Tutor: Student Progress Summary", full_alert_html_body, "AI Tutor Monitoring")
            print(f"SCHEDULER (Progress Check): Sent progress alert summary to {instructor_email}.")
        else: print(f"SCHEDULER (Progress Check): No new low scores to report for instructor {instructor_email} after filtering.")
    print(f"SCHEDULER (Progress Check): Finished.")

def _get_syllabus_text_from_config(course_name_str): # (No changes needed)
    if not course_name_str: return "Error: Course name missing."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try: return generate_syllabus(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e: return f"Error loading syllabus: {e}"

def _get_plan_text_from_config(course_name_str): # (No changes needed)
    if not course_name_str: return "Error: Course name missing."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try: return json.loads(path.read_text(encoding="utf-8")).get("lesson_plan_formatted", "Plan not generated or not found in config.")
    except Exception as e: return f"Error loading plan: {e}"

def enable_edit_syllabus_and_reload(current_course_name, current_output_content): # (No changes needed)
    if not current_output_content.strip().startswith("**Course:**"): syllabus_text = _get_syllabus_text_from_config(current_course_name); return gr.update(value=syllabus_text, interactive=True)
    return gr.update(interactive=True)

def enable_edit_plan_and_reload(current_course_name_for_plan, current_plan_output_content): # (No changes needed)
    is_error_or_status = current_plan_output_content.strip().startswith("⚠️") or current_plan_output_content.strip().startswith("✅") or not current_plan_output_content.strip().startswith("**Course Week")
    if is_error_or_status: plan_text = _get_plan_text_from_config(current_course_name_for_plan); return gr.update(value=plan_text, interactive=True)
    return gr.update(interactive=True)

# MODIFIED save_setup to handle pdf_filepath_str
def save_setup(course_name, instr_name, instr_email, devices_cb_group, 
               pdf_filepath_str, # MODIFIED: This is now a filepath string
               start_year, start_month, start_day, end_year, end_month, end_day,
               class_days_checklist, students_csv_text):
    
    num_expected_outputs = 14 # Adjusted for current_course_name_state
    def error_return_tuple(error_message_str):
        return (gr.update(value=error_message_str, visible=True, interactive=False), gr.update(visible=True), gr.Button.update(visible=True), gr.Button.update(visible=False), gr.Button.update(visible=False), gr.Button.update(visible=False), gr.Button.update(visible=False), gr.Row.update(visible=False), gr.Row.update(visible=False), gr.Textbox.update(value="", visible=False), gr.Markdown.update(visible=True), gr.Textbox.update(value=course_name if course_name else "", visible=False), gr.State.update(value=course_name if course_name else ""))

    try:
        required_fields = {"Course Name": course_name, "Instructor Name": instr_name, "Instructor Email": instr_email, "PDF Material": pdf_filepath_str, "Start Year": start_year, "Start Month": start_month, "Start Day": start_day, "End Year": end_year, "End Month": end_month, "End Day": end_day, "Class Days": class_days_checklist}
        missing = [name for name, val in required_fields.items() if not val]
        if missing: return error_return_tuple(f"⚠️ Error: Required fields missing: {', '.join(missing)}.")

        try:
            start_dt_obj, end_dt_obj = datetime(int(start_year), int(start_month), int(start_day)).date(), datetime(int(end_year), int(end_month), int(end_day)).date()
            if end_dt_obj <= start_dt_obj: return error_return_tuple("⚠️ Error: Course end date must be after the start date.")
        except ValueError: return error_return_tuple("⚠️ Error: Invalid date selected for course schedule.")

        sections_for_desc = []
        full_pdf_text_content = ""
        char_offset_to_page_map_list = []
        
        # MODIFIED: Open the PDF from the filepath
        try:
            with open(pdf_filepath_str, "rb") as actual_pdf_file_obj:
                sections_for_desc = split_sections(actual_pdf_file_obj)
                if not sections_for_desc or (len(sections_for_desc) == 1 and "Error" in sections_for_desc[0].get('title', '')):
                    return error_return_tuple("⚠️ Error: Could not extract structural sections from PDF. Try a different PDF.")

                actual_pdf_file_obj.seek(0) # Reset for full text extraction
                current_char_offset_val = 0
                fitz_used_for_full_text = False
                if fitz_available:
                    doc_for_full_text_extraction = None
                    try:
                        # fitz.open can take a stream
                        doc_for_full_text_extraction = fitz.open(stream=actual_pdf_file_obj.read(), filetype="pdf")
                        actual_pdf_file_obj.seek(0) # Reset pointer
                        if doc_for_full_text_extraction:
                            for page_num_fitz, page_obj_fitz in enumerate(doc_for_full_text_extraction):
                                page_text_content = page_obj_fitz.get_text("text", sort=True)
                                if page_text_content:
                                    char_offset_to_page_map_list.append((current_char_offset_val, page_num_fitz + 1))
                                    full_pdf_text_content += page_text_content + "\n"
                                    current_char_offset_val += len(page_text_content) + 1
                            doc_for_full_text_extraction.close()
                            fitz_used_for_full_text = True
                    except Exception as e_fitz_full_text: print(f"Error extracting full text with fitz: {e_fitz_full_text}.")
                
                if not fitz_used_for_full_text or not full_pdf_text_content.strip():
                    print("Warning (save_setup): Fitz not used or failed for full text. Using concatenated section content.")
                    full_pdf_text_content = "\n\n".join(s['content'] for s in sections_for_desc)
                    char_offset_to_page_map_list = [] # Page map likely lost
                    if sections_for_desc and sections_for_desc[0].get('page') is not None:
                        temp_offset = 0
                        for s_item in sections_for_desc: char_offset_to_page_map_list.append((temp_offset, s_item['page'])); temp_offset += len(s_item['content']) + 2
        except FileNotFoundError: return error_return_tuple(f"⚠️ Error: Uploaded PDF file not found at path: {pdf_filepath_str}")
        except Exception as e_pdf_open: return error_return_tuple(f"⚠️ Error processing PDF file: {e_pdf_open}")

        if not full_pdf_text_content.strip(): return error_return_tuple("⚠️ Error: Extracted PDF text content is empty.")

        content_sample_for_ai = "\n\n".join(f"Section Title (Page {s.get('page', 'N/A')}): {s['title']}\nContent Snippet: {s['content'][:500]}..." for s in sections_for_desc[:5])
        if len(content_sample_for_ai) > 8000: content_sample_for_ai = content_sample_for_ai[:8000] + "..."
        
        client = openai.OpenAI()
        try:
            desc_response = client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "system", "content": "Based on the following course material snippets, generate a concise and engaging course description (2-3 sentences max)."}, {"role": "user", "content": content_sample_for_ai}])
            course_desc_text = desc_response.choices[0].message.content.strip()
            obj_response = client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "system", "content": "Based on the following course material snippets, generate 5-7 clear, actionable learning objectives. Each objective should start with an action verb. List them one per line."}, {"role": "user", "content": content_sample_for_ai}])
            learning_objs_list = [line.strip(" -•*") for line in obj_response.choices[0].message.content.splitlines() if line.strip()]
        except openai.APIError as oai_err: print(f"OpenAI API Error: {oai_err}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ OpenAI API Error: {oai_err}.")

        parsed_students_list = []
        if students_csv_text:
            for line_num, line_str in enumerate(students_csv_text.splitlines()):
                if line_str.strip():
                    parts = [p.strip() for p in line_str.split(',', 1)];
                    if len(parts) >= 2 and parts[0] and "@" in parts[1]: parsed_students_list.append({"id": str(uuid.uuid4()), "name": parts[0], "email": parts[1]})
                    else: print(f"Warning (save_setup): Skipping invalid student line {line_num+1}: '{line_str}'")
        
        config_data = {"course_name": course_name, "instructor": {"name": instr_name, "email": instr_email}, "class_days": class_days_checklist, "start_date": start_dt_obj.strftime('%Y-%m-%d'), "end_date": end_dt_obj.strftime('%Y-%m-%d'), "allowed_devices": devices_cb_group, "students": parsed_students_list, "full_text_content": full_pdf_text_content, "char_offset_page_map": char_offset_to_page_map_list, "course_description": course_desc_text, "learning_objectives": learning_objs_list, "lessons": [], "lesson_plan_formatted": ""}
        config_file_path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        config_file_path.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")
        syllabus_display_text = generate_syllabus(config_data)
        
        return (gr.update(value=syllabus_display_text, visible=True, interactive=False), gr.update(visible=False), gr.Button.update(visible=True), gr.Button.update(visible=True), gr.Button.update(visible=True), gr.Button.update(visible=False), gr.Button.update(visible=False), gr.Row.update(visible=True), gr.Row.update(visible=True), gr.Textbox.update(value="Lesson plan not yet generated.", visible=False), gr.Markdown.update(visible=False), gr.Textbox.update(value=course_name, visible=True), gr.State.update(value=course_name))
    except Exception as e: print(f"Error in save_setup: {e}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ An unexpected error occurred: {e}")

def generate_plan_callback(course_name_for_plan): # (No changes needed here from your last version)
    num_expected_outputs_plan = 6 # Adjusted
    def error_return_for_plan_gen(error_message_str): return (gr.update(value=error_message_str, visible=True, interactive=False), gr.Button.update(visible=True), gr.Button.update(visible=False), gr.Button.update(visible=False))
    try:
        if not course_name_for_plan: return error_return_for_plan_gen("⚠️ Error: Course Name is required.")
        config_file_path = CONFIG_DIR / f"{course_name_for_plan.replace(' ','_').lower()}_config.json"
        if not config_file_path.exists(): return error_return_for_plan_gen(f"⚠️ Error: Config for '{course_name_for_plan}' not found.")
        cfg_data = json.loads(config_file_path.read_text(encoding="utf-8"))
        if not cfg_data.get("full_text_content") or not cfg_data.get("start_date") or not cfg_data.get("end_date") or not cfg_data.get("class_days"): return error_return_for_plan_gen("⚠️ Error: Course config incomplete.")
        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg_data)
        if "Error:" in formatted_plan_str : return error_return_for_plan_gen(f"⚠️ Error generating plan: {formatted_plan_str}")
        cfg_data["lessons"], cfg_data["lesson_plan_formatted"] = structured_lessons_list, formatted_plan_str
        config_file_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8")
        class_days_str = ", ".join(cfg_data.get("class_days", ["as per configured schedule"]))
        notification_message_for_plan = f"\n\n---\n✅ **Lesson Plan Generated & Daily Email Reminders Activated!**\nStudents in this course will now receive email reminders with a unique link to their AI Tutor lesson on each scheduled class day ({class_days_str}). Links are typically active from 6:00 AM to 12:00 PM UTC on those days."
        display_text_for_plan_output_box = formatted_plan_str + notification_message_for_plan
        return (gr.update(value=display_text_for_plan_output_box, visible=True, interactive=False), gr.Button.update(visible=True, value="Re-generate Lesson Plan"), gr.Button.update(visible=True), gr.Button.update(visible=True))
    except openai.APIError as oai_err: print(f"OpenAI API Error: {oai_err}\n{traceback.format_exc()}"); return error_return_for_plan_gen(f"⚠️ OpenAI API Error: {oai_err}.")
    except Exception as e: print(f"Error in generate_plan_callback: {e}\n{traceback.format_exc()}"); return error_return_for_plan_gen(f"⚠️ An unexpected error occurred: {e}")

def email_document_callback(course_name_str, document_type_str, output_text_content_str, students_text_input_str):
    # This function will use the modified send_email_notification which expects attachment_filepath_str
    # However, download_docx returns a BytesIO buffer. We need to save this buffer to a temp file
    # to pass its path to send_email_notification, or modify send_email_notification to also handle BytesIO.
    # For now, let's save to a temp file.
    if not SMTP_USER or not SMTP_PASS: return gr.update(value="⚠️ Error: SMTP settings not configured.")
    status_message_prefix = f"Emailing {document_type_str}"
    try:
        if not course_name_str or not output_text_content_str: return gr.update(value=f"⚠️ Error: Course Name & {document_type_str} content required.")
        config_file_path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
        if not config_file_path.exists(): return gr.update(value=f"⚠️ Error: Config for '{course_name_str}' not found.")
        cfg_data = json.loads(config_file_path.read_text(encoding="utf-8")); instructor_info, instructor_name, instructor_email = cfg_data.get("instructor", {}), cfg_data.get("instructor", {}).get("name", "Instructor"), cfg_data.get("instructor", {}).get("email")
        
        docx_buffer, docx_filename = download_docx(output_text_content_str, f"{course_name_str.replace(' ','_')}_{document_type_str.lower().replace(' ','_')}.docx")
        
        # Save buffer to a temporary file to get a filepath
        temp_dir = Path("temp_attachments")
        temp_dir.mkdir(exist_ok=True)
        temp_filepath = temp_dir / docx_filename
        with open(temp_filepath, "wb") as f_temp:
            f_temp.write(docx_buffer.getvalue())

        recipients_list = []
        if instructor_email: recipients_list.append({"name": instructor_name, "email": instructor_email})
        if students_text_input_str:
            for line_str in students_text_input_str.splitlines():
                if line_str.strip(): parts = [p.strip() for p in line_str.split(',', 1)];
                if len(parts) == 2 and parts[0] and "@" in parts[1]: recipients_list.append({"name": parts[0], "email": parts[1]})
        if not recipients_list: os.remove(temp_filepath); return gr.update(value="⚠️ Error: No valid recipients found.")

        successful_sends, error_messages_list = 0, []
        email_subject = f"{document_type_str.capitalize()} for Course: {course_name_str}"
        email_body_html = f"<html><body><p>Dear Recipient,</p><p>Please find attached the {document_type_str.lower()} for the course: <strong>{course_name_str}</strong>.</p><p>Best regards,<br>AI Tutor System ({instructor_name})</p></body></html>"

        for recipient in recipients_list:
            print(f"{status_message_prefix}: Sending to {recipient['name']} <{recipient['email']}>...")
            if send_email_notification(to_email=recipient["email"], subject=email_subject, html_content=email_body_html, from_name=f"AI Tutor ({instructor_name})", attachment_filepath_str=str(temp_filepath), attachment_filename_override=docx_filename): successful_sends += 1
            else: error_messages_list.append(f"Failed to send to {recipient['email']}.")
        
        os.remove(temp_filepath) # Clean up temp file

        final_status_message = f"✅ {document_type_str.capitalize()} email attempts finished. Sent to {successful_sends}/{len(recipients_list)}."
        if error_messages_list: final_status_message += f"\n⚠️ Errors:\n" + "\n".join(error_messages_list)
        return gr.update(value=final_status_message)
    except Exception as e: err_text = f"⚠️ Unexpected error during {status_message_prefix}:\n{e}\n{traceback.format_exc()}"; print(err_text); return gr.update(value=err_text)

def email_syllabus_callback(c, s_str, out_content): return email_document_callback(c, "Syllabus", out_content, s_str)
def email_plan_callback(c, s_str, out_content): return email_document_callback(c, "Lesson Plan", out_content, s_str)

# --- Build Instructor UI (MODIFIED gr.File types) ---
def build_instructor_ui():
    with gr.Blocks(theme=gr.themes.Soft(primary_hue=gr.themes.colors.blue, secondary_hue=gr.themes.colors.sky)) as instructor_panel_ui:
        gr.Markdown("## AI Tutor Instructor Panel")
        current_course_name_state = gr.State("")
        with gr.Tabs():
            with gr.TabItem("1. Course Setup & Syllabus", id="tab_setup"):
                gr.Markdown("### Create or Update Course Configuration and Generate Syllabus")
                with gr.Row(): course_name_input, instr_name_input, instr_email_input = gr.Textbox(label="Course Name*"), gr.Textbox(label="Instructor Name*"), gr.Textbox(label="Instructor Email*", type="email")
                # MODIFIED: type="filepath"
                pdf_upload_component = gr.File(label="Upload Course Material PDF*", file_types=[".pdf"], type="filepath")
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("#### Course Schedule"); current_year = datetime.now().year; year_choices, month_choices, day_choices = [str(y) for y in range(current_year, current_year + 5)], [f"{m:02d}" for m in range(1, 13)], [f"{d:02d}" for d in range(1, 32)]
                        with gr.Row(): start_year_dd, start_month_dd, start_day_dd = gr.Dropdown(year_choices, label="Start Year*"), gr.Dropdown(month_choices, label="Start Month*"), gr.Dropdown(day_choices, label="Start Day*")
                        with gr.Row(): end_year_dd, end_month_dd, end_day_dd = gr.Dropdown(year_choices, label="End Year*"), gr.Dropdown(month_choices, label="End Month*"), gr.Dropdown(day_choices, label="End Day*")
                        class_days_cb_group = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*", info="Select days of the week classes are held.")
                    with gr.Column(scale=1):
                        gr.Markdown("#### Student & Access Details"); allowed_devices_cb_group = gr.CheckboxGroup(["Phone", "PC", "Tablet"], label="Allowed Devices for Tutor", value=["PC", "Tablet"])
                        students_textbox = gr.Textbox(label="Student List (One per line: Name,Email)", lines=5, placeholder="Example One,student.one@example.com\nExample Two,student.two@example.com", info="Enter student names and emails, separated by a comma.")
                save_setup_button = gr.Button("1. Save Setup & Generate Syllabus", variant="primary", icon="💾"); gr.Markdown("---")
                syllabus_output_textbox = gr.Textbox(label="Syllabus Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                with gr.Row(visible=False) as syllabus_actions_row_ui: edit_syllabus_button, email_syllabus_button = gr.Button(value="📝 Edit Syllabus Text"), gr.Button(value="📧 Email Syllabus to All", variant="secondary")
            with gr.TabItem("2. Lesson Plan Management", id="tab_plan"):
                lesson_plan_initial_message_md = gr.Markdown(value="### Course Setup Required\nComplete 'Course Setup & Syllabus' on Tab 1 first.", visible=True)
                course_name_for_plan_display = gr.Textbox(label="Active Course for Lesson Plan", interactive=False, visible=False)
                generate_plan_button = gr.Button("2. Generate Lesson Plan", variant="primary", icon="📅", visible=False)
                lesson_plan_output_textbox = gr.Textbox(label="Lesson Plan Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                with gr.Row(visible=False) as plan_actions_row_ui: edit_plan_button, email_plan_button = gr.Button(value="📝 Edit Plan Text"), gr.Button(value="📧 Email Plan to All", variant="secondary")
            with gr.TabItem("Contact Support", id="tab_support"):
                gr.Markdown("### Send a Message to Support Team")
                with gr.Row(): contact_submitter_name, contact_submitter_email = gr.Textbox(label="Your Name"), gr.Textbox(label="Your Email Address", type="email")
                contact_message_body = gr.Textbox(label="Message Body", lines=7, placeholder="Describe your issue or query...")
                # MODIFIED: type="filepath"
                contact_file_attachment = gr.File(label="Attach File (Optional)", file_count="single", type="filepath")
                send_contact_email_button = gr.Button("Send Message to Support", variant="primary", icon="✉️"); contact_status_md = gr.Markdown(value="")
                def handle_contact_form_submission(submitter_name, submitter_email, message_body, file_attachment_path): # MODIFIED: file_attachment_path
                    validation_errors = []
                    if not submitter_name.strip(): validation_errors.append("Your Name is required.")
                    if not submitter_email.strip(): validation_errors.append("Your Email Address is required.")
                    elif "@" not in submitter_email: validation_errors.append("Please enter a valid Email Address.")
                    if not message_body.strip(): validation_errors.append("Message Body cannot be empty.")
                    if validation_errors: return (gr.update(value="<span style='color:red;'>Please fix errors:</span>\n" + "\n".join(f" - {err}" for err in validation_errors)), submitter_name, submitter_email, message_body, file_attachment_path)
                    email_subject = f"AI Tutor Support Request from: {submitter_name} <{submitter_email}>"
                    html_message_body = f"<p><strong>From:</strong> {submitter_name} ({submitter_email})</p><p><strong>Message:</strong></p><pre>{message_body.replace('<','<').replace('>','>')}</pre>"
                    email_sent_successfully = send_email_notification(to_email=SUPPORT_EMAIL_ADDRESS, subject=email_subject, html_content=html_message_body, from_name=submitter_email, attachment_filepath_str=file_attachment_path) # MODIFIED: pass filepath
                    if email_sent_successfully: return (gr.update(value="<p style='color:green;'>Message sent successfully! ✔</p>"), gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=None))
                    else: return (gr.update(value="<p style='color:red;'>⚠️ Message could not be sent.</p>"), submitter_name, submitter_email, message_body, file_attachment_path)
                send_contact_email_button.click(handle_contact_form_submission, inputs=[contact_submitter_name, contact_submitter_email, contact_message_body, contact_file_attachment], outputs=[contact_status_md, contact_submitter_name, contact_submitter_email, contact_message_body, contact_file_attachment], queue=True)
        
        save_setup_button.click(save_setup, inputs=[course_name_input, instr_name_input, instr_email_input, allowed_devices_cb_group, pdf_upload_component, start_year_dd, start_month_dd, start_day_dd, end_year_dd, end_month_dd, end_day_dd, class_days_cb_group, students_textbox], outputs=[syllabus_output_textbox, save_setup_button, generate_plan_button, edit_syllabus_button, email_syllabus_button, edit_plan_button, email_plan_button, syllabus_actions_row_ui, plan_actions_row_ui, lesson_plan_output_textbox, lesson_plan_initial_message_md, course_name_for_plan_display, current_course_name_state])
        course_name_input.change(lambda val: (gr.update(value=val), val), inputs=[course_name_input], outputs=[course_name_for_plan_display, current_course_name_state])
        edit_syllabus_button.click(enable_edit_syllabus_and_reload, inputs=[current_course_name_state, syllabus_output_textbox], outputs=[syllabus_output_textbox])
        email_syllabus_button.click(email_syllabus_callback, inputs=[current_course_name_state, students_textbox, syllabus_output_textbox], outputs=[syllabus_output_textbox], queue=True)
        generate_plan_button.click(generate_plan_callback, inputs=[current_course_name_state], outputs=[lesson_plan_output_textbox, generate_plan_button, edit_plan_button, email_plan_button]).then(lambda: (gr.update(visible=True), gr.update(visible=True)), inputs=None, outputs=[lesson_plan_output_textbox, plan_actions_row_ui])
        edit_plan_button.click(enable_edit_plan_and_reload, inputs=[current_course_name_state, lesson_plan_output_textbox], outputs=[lesson_plan_output_textbox])
        email_plan_button.click(email_plan_callback, inputs=[current_course_name_state, students_textbox, lesson_plan_output_textbox], outputs=[lesson_plan_output_textbox], queue=True)
    return instructor_panel_ui

instructor_ui_instance = build_instructor_ui()
app = gr.mount_gradio_app(app, instructor_ui_instance, path="/instructor")

@app.get("/")
def root_redirect(): return RedirectResponse(url="/instructor")

# --- Student Tutor UI and Logic (No changes needed here from your last version for this specific error) ---
def build_student_tutor_ui(): # (No changes needed here from your last version)
    def generate_student_system_prompt(mode, student_interests_str, current_topic, current_segment_text):
        segment_preview = current_segment_text[:1000] + "..." if len(current_segment_text) > 1000 else current_segment_text
        base_prompt = f"You are {STUDENT_BOT_NAME}, a friendly, patient, and encouraging AI English tutor. Your student's target English level is approximately {STUDENT_DEFAULT_ENGLISH_LEVEL}. Keep your responses concise, clear, and directly related to the student's input or the current learning mode. Always aim to be helpful and supportive."
        if mode == "initial_greeting": return f"{base_prompt} Today's lesson is about: '{current_topic}'. Let's start by getting to know each other a bit. What are some of your hobbies or interests?"
        elif mode == "onboarding": return f"{base_prompt} You are continuing to get to know the student. Their interests mentioned so far include: {student_interests_str if student_interests_str else 'none yet'}. Ask another friendly, open-ended question to learn more about their preferences or daily life."
        elif mode == "teaching_transition": return f"{base_prompt} Student's interests: {student_interests_str if student_interests_str else 'various topics'}. Now, let's smoothly transition to our main topic for today: '{current_topic}'. This lesson is based on the following material: \"{segment_preview}\" To start, what do you already know or think about '{current_topic}'?"
        elif mode == "teaching": return f"{base_prompt} You are currently teaching the student about '{current_topic}'. Refer to this text segment: \"{segment_preview}\". Relate to student interests ({student_interests_str if student_interests_str else 'general examples'}) if appropriate. Provide gentle corrections if they make mistakes. End your response with a question to encourage further interaction or check understanding."
        elif mode == "interest_break_transition": return f"{base_prompt} Great work so far! Let's take a very short break from '{current_topic}'. Thinking about your interests ({student_interests_str if student_interests_str else 'something fun'}), ask a light, engaging question or share a quick, interesting fact related to those interests."
        elif mode == "interest_break_active": return f"{base_prompt} The student has responded during the interest break. Give a brief, positive, and engaging reply. Then, gently guide the conversation back to the main lesson topic: '{current_topic}'."
        elif mode == "quiz_time": return f"{base_prompt} Alright, it's time for a quick quiz question on '{current_topic}'! Based on our discussion and this text: \"{segment_preview}\", I'll ask you a multiple-choice question. Please choose the best answer (A, B, or C).\n\nInternally, you MUST generate one clear multiple-choice question with three distinct options (A, B, C) and clearly identify the correct answer. Example for internal thought: 'Question: What is X? A) Opt1 B) Opt2 C) Opt3. Correct: B'. Present only the question and options to the student."
        elif mode == "ending_session": return f"{base_prompt} We're nearing the end of our session on '{current_topic}'. Thank you for your participation! Briefly summarize what was covered or offer a final encouraging thought."
        elif mode == "error": return f"You are {STUDENT_BOT_NAME}. There has been an error loading the lesson. Apologize and ask the student to refresh or contact support."
        return base_prompt
    with gr.Blocks(theme=gr.themes.Soft(primary_hue=gr.themes.colors.teal, secondary_hue=gr.themes.colors.cyan)) as student_tutor_demo_ui:
        st_token_val, st_course_id_val, st_lesson_id_val, st_student_id_val = gr.State(None), gr.State(None), gr.State(None), gr.State(None)
        st_lesson_topic_val, st_lesson_segment_text_val = gr.State("Loading lesson..."), gr.State("Loading content...")
        st_chat_history_list, st_display_history_list = gr.State([]), gr.State([])
        st_student_profile_dict = gr.State({"interests": [], "quiz_score": {"correct": 0, "total": 0, "last_question_correct_answer": None}})
        st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int, st_session_start_time_dt = gr.State("initial_greeting"), gr.State(0), gr.State(0), gr.State(None)
        title_markdown = gr.Markdown(f"# {STUDENT_BOT_NAME}")
        with gr.Row():
            with gr.Column(scale=1, min_width=200): voice_selector_dropdown, mic_audio_input, text_message_input, send_message_button = gr.Dropdown(choices=["alloy", "echo", "fable", "onyx", "nova", "shimmer"], value="nova", label="Tutor Voice"), gr.Audio(sources=["microphone"], type="filepath", label="Record your response:", elem_id="student_mic_input"), gr.Textbox(label="Or type your response here:", placeholder="Type and press Enter to send...", elem_id="student_text_input"), gr.Button("Send Message", variant="primary", icon="💬")
            with gr.Column(scale=3, min_width=400): main_chatbot_interface, tutor_audio_output = gr.Chatbot(label=f"Conversation with {STUDENT_BOT_NAME}", height=550, bubble_full_width=False, show_copy_button=True), gr.Audio(type="filepath", autoplay=False, label=f"{STUDENT_BOT_NAME} says:", elem_id="tutor_audio_player")
        def st_initialize_session(request: gr.Request):
            token_from_url_params = request.query_params.get("token")
            initial_updates = {title_markdown: gr.update(value=f"# {STUDENT_BOT_NAME} - Error"), st_lesson_topic_val: "Error", st_lesson_segment_text_val: "Error loading", st_session_mode_str: "error", st_session_start_time_dt: datetime.now(dt_timezone.utc), main_chatbot_interface: [[None, "Error: Could not initialize lesson. Please check the URL or contact support."]], st_chat_history_list: [], st_display_history_list: [[None, "Error."]]}
            if not token_from_url_params: initial_updates[main_chatbot_interface], initial_updates[st_display_history_list] = [[None, "Error: Access token missing."]], [[None, "Error: Access token missing."]]; return initial_updates
            try:
                payload = jwt.decode(token_from_url_params, JWT_SECRET_KEY, algorithms=[ALGORITHM], audience=APP_DOMAIN); s_id, c_id, l_id = payload["sub"], payload["course_id"], int(payload["lesson_id"])
                config_path = CONFIG_DIR / f"{c_id.replace(' ','_').lower()}_config.json";
                if not config_path.exists(): raise FileNotFoundError("Course configuration not found.")
                cfg = json.loads(config_path.read_text(encoding="utf-8")); full_text, lessons_data = cfg.get("full_text_content", ""), cfg.get("lessons", [])
                if not full_text or not lessons_data: raise ValueError("Lesson content or plan is missing.")
                if not (1 <= l_id <= len(lessons_data)): raise ValueError(f"Lesson ID {l_id} out of range.")
                l_topic = lessons_data[l_id - 1].get("topic_summary", f"Lesson {l_id}"); num_total_lessons = len(lessons_data)
                chars_per_segment = len(full_text) // num_total_lessons if num_total_lessons > 0 else len(full_text)
                start_char_index, end_char_index = (l_id - 1) * chars_per_segment, l_id * chars_per_segment if l_id < num_total_lessons else len(full_text)
                l_segment_text = full_text[start_char_index:end_char_index].strip();
                if not l_segment_text: l_segment_text = "(No specific text segment for this lesson.)"
                client = openai.OpenAI(); initial_system_prompt = generate_student_system_prompt("initial_greeting", "", l_topic, l_segment_text)
                llm_response = client.chat.completions.create(model=STUDENT_CHAT_MODEL, messages=[{"role": "system", "content": initial_system_prompt}], max_tokens=150, temperature=0.7)
                initial_tutor_msg_text = llm_response.choices[0].message.content.strip(); current_chat_hist, current_display_hist = [{"role": "assistant", "content": initial_tutor_msg_text}], [[None, initial_tutor_msg_text]]
                tts_audio_path_update = None
                try: tts_response_obj = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice="nova", input=initial_tutor_msg_text); intro_audio_filepath = STUDENT_AUDIO_DIR / f"init_greeting_{uuid.uuid4()}.mp3"; tts_response_obj.stream_to_file(intro_audio_filepath); tts_audio_path_update = gr.update(value=str(intro_audio_filepath), autoplay=True)
                except Exception as e_tts_init: print(f"STUDENT_TUTOR_INIT: TTS for initial greeting failed: {e_tts_init}")
                initial_updates.update({title_markdown: gr.update(value=f"# {STUDENT_BOT_NAME} - Lesson: {l_topic}"), st_token_val: token_from_url_params, st_course_id_val: c_id, st_lesson_id_val: l_id, st_student_id_val: s_id, st_lesson_topic_val: l_topic, st_lesson_segment_text_val: l_segment_text, st_chat_history_list: current_chat_hist, st_display_history_list: current_display_hist, main_chatbot_interface: gr.update(value=current_display_hist), tutor_audio_output: tts_audio_path_update, st_session_mode_str: "onboarding", st_turn_count_int: 0, st_teaching_turns_count_int: 0, st_session_start_time_dt: datetime.now(dt_timezone.utc)})
                return initial_updates
            except jwt.ExpiredSignatureError: error_text = "Access token has expired."
            except jwt.InvalidTokenError: error_text = "Invalid access token."
            except FileNotFoundError as e_fnf: error_text = f"Course data error: {e_fnf}."
            except ValueError as e_val: error_text = f"Lesson data error: {e_val}."
            except openai.APIError as e_oai: error_text = f"AI service unavailable: {e_oai}."; print(f"STUDENT_TUTOR_INIT: OpenAI API Error: {e_oai}\n{traceback.format_exc()}")
            except Exception as e_gen: error_text = f"Unexpected error loading lesson: {e_gen}."; print(f"STUDENT_TUTOR_INIT: General Exception: {e_gen}\n{traceback.format_exc()}")
            initial_updates[main_chatbot_interface], initial_updates[st_display_history_list] = [[None, error_text]], [[None, error_text]]; return initial_updates
        def st_process_student_turn(mic_filepath, typed_input_text, current_chat_hist, current_display_hist, current_profile, current_mode, current_turns, current_teaching_turns, selected_tutor_voice, active_s_id, active_c_id, active_l_id, active_l_topic, active_l_segment_text, session_start_dt):
            if current_mode == "error": return current_display_hist, current_chat_hist, current_profile, current_mode, current_turns, current_teaching_turns, gr.update(value=None), gr.update(value=None), gr.update(value="")
            student_input_text, client = "", openai.OpenAI()
            if mic_filepath:
                try: 
                    with open(mic_filepath, "rb") as audio_file_obj: student_input_text = client.audio.transcriptions.create(file=audio_file_obj, model=STUDENT_WHISPER_MODEL).text.strip()
                    if not student_input_text: student_input_text = "(No speech detected)"
                except Exception as e_stt: student_input_text = f"(Audio transcription error: {e_stt})"; print(f"STUDENT_TUTOR_STT_ERROR: {e_stt}")
                try: os.remove(mic_filepath)
                except OSError: pass
            elif typed_input_text: student_input_text = typed_input_text.strip()
            if not student_input_text: return current_display_hist, current_chat_hist, current_profile, current_mode, current_turns, current_teaching_turns, gr.update(value=None), gr.update(value=None), gr.update(value="")
            current_display_hist.append([student_input_text, None]); current_chat_hist.append({"role": "user", "content": student_input_text}); current_turns += 1
            next_session_mode = current_mode
            if current_mode == "onboarding": current_profile["interests"].append(student_input_text);
            if current_turns >= STUDENT_ONBOARDING_TURNS: next_session_mode = "teaching_transition"
            elif current_mode == "teaching_transition": next_session_mode = "teaching"
            elif current_mode == "teaching":
                current_teaching_turns += 1
                if current_teaching_turns > 0 and current_teaching_turns % STUDENT_QUIZ_AFTER_TURNS == 0: next_session_mode = "quiz_time"
                elif current_teaching_turns > 0 and current_teaching_turns % STUDENT_TEACHING_TURNS_PER_BREAK == 0 : next_session_mode = "interest_break_transition"
            elif current_mode == "interest_break_transition": next_session_mode = "interest_break_active"
            elif current_mode == "interest_break_active": next_session_mode = "teaching"
            elif current_mode == "quiz_time":
                current_profile["quiz_score"]["total"] += 1; last_correct_ans = current_profile.get("last_question_correct_answer", "").upper()
                if last_correct_ans and last_correct_ans in student_input_text.upper(): current_profile["quiz_score"]["correct"] += 1
                current_profile["last_question_correct_answer"] = None; next_session_mode = "teaching"
            if current_turns >= STUDENT_MAX_SESSION_TURNS: next_session_mode = "ending_session"
            interests_str_for_prompt = ", ".join(current_profile["interests"]) if current_profile["interests"] else "not yet specified"; current_system_prompt = generate_student_system_prompt(next_session_mode, interests_str_for_prompt, active_l_topic, active_l_segment_text)
            tutor_response_text = "I'm processing..."
            try:
                messages_for_llm_api = [{"role": "system", "content": current_system_prompt}] + current_chat_hist
                llm_api_response = client.chat.completions.create(model=STUDENT_CHAT_MODEL, messages=messages_for_llm_api, max_tokens=250, temperature=0.7)
                tutor_response_text = llm_api_response.choices[0].message.content.strip()
                if next_session_mode == "quiz_time": match_correct = re.search(r"[Cc]orrect [Aa]nswer:?\s*([A-Ca-c])", tutor_response_text); current_profile["last_question_correct_answer"] = match_correct.group(1).upper() if match_correct else None
            except openai.APIError as e_oai_chat: print(f"STUDENT_TUTOR: OpenAI chat API error: {e_oai_chat}"); tutor_response_text = "I encountered an issue. Could you rephrase?"
            except Exception as e_llm_gen: print(f"STUDENT_TUTOR: LLM response error: {e_llm_gen}"); tutor_response_text = "Apologies, I'm having trouble. Let's try again."
            current_chat_hist.append({"role": "assistant", "content": tutor_response_text}); current_display_hist[-1][1] = tutor_response_text
            tts_audio_path_update = None
            try: tts_response_obj = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice=selected_tutor_voice, input=tutor_response_text); reply_audio_filepath = STUDENT_AUDIO_DIR / f"tutor_reply_{uuid.uuid4()}.mp3"; tts_response_obj.stream_to_file(reply_audio_filepath); tts_audio_path_update = gr.update(value=str(reply_audio_filepath), autoplay=True)
            except Exception as e_tts_reply: print(f"STUDENT_TUTOR: TTS for reply failed: {e_tts_reply}")
            if next_session_mode == "ending_session":
                session_end_dt, duration_in_seconds = datetime.now(dt_timezone.utc), 0
                if session_start_dt: duration_in_seconds = (session_end_dt - session_start_dt).total_seconds()
                quiz_score_display_str = f"{current_profile['quiz_score']['correct']}/{current_profile['quiz_score']['total']}"; engagement_summary_notes = f"Interests: {interests_str_for_prompt if interests_str_for_prompt != 'not yet specified' else 'None'}. Turns: {current_turns}."
                log_student_progress(active_s_id, active_c_id, active_l_id, quiz_score_display_str, duration_in_seconds, engagement_notes=engagement_summary_notes); print(f"STUDENT_TUTOR: Session ended for {active_s_id}. Progress logged.")
            return current_display_hist, current_chat_hist, current_profile, next_session_mode, current_turns, current_teaching_turns, tts_audio_path_update, gr.update(value=None), gr.update(value="")
        student_tutor_demo_ui.load(fn=st_initialize_session, inputs=None, outputs=[title_markdown, st_token_val, st_course_id_val, st_lesson_id_val, st_student_id_val, st_lesson_topic_val, st_lesson_segment_text_val, st_chat_history_list, st_display_history_list, main_chatbot_interface, tutor_audio_output, st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int, st_session_start_time_dt])
        process_turn_inputs = [mic_audio_input, text_message_input, st_chat_history_list, st_display_history_list, st_student_profile_dict, st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int, voice_selector_dropdown, st_student_id_val, st_course_id_val, st_lesson_id_val, st_lesson_topic_val, st_lesson_segment_text_val, st_session_start_time_dt]
        process_turn_outputs = [main_chatbot_interface, st_chat_history_list, st_student_profile_dict, st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int, tutor_audio_output, mic_audio_input, text_message_input]
        mic_audio_input.change(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")
        text_message_input.submit(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")
        send_message_button.click(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")
    return student_tutor_demo_ui

student_tutor_app_instance = build_student_tutor_ui()
app = gr.mount_gradio_app(app, student_tutor_app_instance, path=STUDENT_UI_PATH)

@app.get("/class", response_class=HTMLResponse)
async def route_get_student_lesson_page(request: Request, token: str = None): # (No changes needed)
    if not token: return HTMLResponse("<h3>Error: Access token missing.</h3><p>Please use the unique link provided in your email.</p>", status_code=400)
    try: jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM], audience=APP_DOMAIN, options={"verify_exp": True})
    except jwt.ExpiredSignatureError: return HTMLResponse("<h3>Error: Your lesson access link has expired.</h3>", status_code=401)
    except jwt.InvalidTokenError: return HTMLResponse("<h3>Error: Invalid access link.</h3>", status_code=401)
    except Exception as e: print(f"Unexpected error during token pre-validation for /class: {e}"); return HTMLResponse("<h3>Error: Could not validate access.</h3>", status_code=500)
    return RedirectResponse(url=f"{STUDENT_UI_PATH}?token={token}")

@app.get("/debug/run_reminders_manually")
async def debug_trigger_reminders(): # (No changes needed)
    print("DEBUG: Manually triggering send_daily_class_reminders()...");
    try: send_daily_class_reminders(); return {"status": "ok", "message": "send_daily_class_reminders job executed."}
    except Exception as e: print(f"Error in manual trigger of reminders: {e}\n{traceback.format_exc()}"); return {"status": "error", "message": str(e)}

@app.on_event("startup")
async def startup_event_tasks(): # (No changes needed)
    scheduler.add_job(send_daily_class_reminders, trigger=CronTrigger(hour=5, minute=50, timezone='UTC'), id="daily_class_reminders_job", name="Daily Student Class Reminders", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(check_student_progress_and_notify_professor, trigger=CronTrigger(hour=18, minute=0, timezone='UTC'), id="student_progress_check_job", name="Check Student Progress & Notify Instructor", replace_existing=True, misfire_grace_time=300)
    if not scheduler.running: scheduler.start(); print("APScheduler started with jobs:")
    else: print("APScheduler already running. Current jobs:")
    for job in scheduler.get_jobs(): print(f"  - Job ID: {job.id}, Name: {job.name}, Next Run: {job.next_run_time}")

@app.on_event("shutdown")
async def shutdown_event_tasks(): # (No changes needed)
    if scheduler.running: scheduler.shutdown(); print("APScheduler shutdown.")

if __name__ == "__main__":
    import uvicorn
    print(f"Starting AI Tutor Application...")
    uvicorn.run("ai_tutor_full:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True) # Assuming your file is ai_tutor_full.py for Render
