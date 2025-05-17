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
import mimetypes
import csv # For simple progress logging

import openai
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates # For serving the student UI shell
from fastapi.staticfiles import StaticFiles # If you have static assets for student UI

import jwt
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.middleware.cors import CORSMiddleware

# ─── Create FastAPI app & CORS ───────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# --- APScheduler Setup & Jobs ---
scheduler = BackgroundScheduler(timezone="UTC") 

# Health‐check endpoint (only on localhost once you bind to 127.0.0.1)
@app.get("/healthz")
def healthz():
    return {"status": "ok", "scheduler_running": scheduler.running}

# Mount your Gradio Instructor UI under /instructor 
instructor_ui = build_instructor_ui()
app = gr.mount_gradio_app(app, instructor_ui, path="/instructor")

# Redirect root (/) → /instructor so users just type your domain 
@app.get("/")
def root():
    return RedirectResponse(url="/instructor")

# Attempt to import fitz (PyMuPDF)
try:
    import fitz
    fitz_available = True
except ImportError:
    fitz_available = False
    print("PyMuPDF (fitz) not found. Page number mapping will be limited.")

# --- Configuration ---
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)
PROGRESS_LOG_FILE = CONFIG_DIR / "student_progress_log.csv" # For simplified progress

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "a-very-secure-secret-key-please-change")
if JWT_SECRET_KEY == "a-very-secure-secret-key-please-change":
    print("WARNING: JWT_SECRET_KEY is set to default. Set a strong secret key in env variables.")
LINK_VALIDITY_HOURS = 6
ALGORITHM = "HS256"

EASYAI_TUTOR_PROGRESS_API_ENDPOINT = os.getenv("EASYAI_TUTOR_PROGRESS_API_ENDPOINT") # Less used now

days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

# --- Student Tutor Configuration (can be moved to a separate config if needed) ---
STUDENT_TTS_MODEL = "tts-1"
STUDENT_CHAT_MODEL = "gpt-4o-mini" # Or gpt-3.5-turbo for cost/speed
STUDENT_WHISPER_MODEL = "whisper-1"
STUDENT_DEFAULT_ENGLISH_LEVEL = "B1 (Intermediate)" 
STUDENT_AUDIO_DIR = Path("student_audio_files")
STUDENT_AUDIO_DIR.mkdir(exist_ok=True)
STUDENT_BOT_NAME = "Easy AI Tutor"
STUDENT_LOGO_PATH = "logo.png" # Ensure this path is accessible or remove

STUDENT_ONBOARDING_TURNS = 2 # User turns for onboarding
STUDENT_TEACHING_TURNS_PER_BREAK = 5 # User turns in teaching before interest break
STUDENT_INTEREST_BREAK_TURNS = 1 # User turns during an interest break
STUDENT_QUIZ_AFTER_TURNS = 7 # User teaching turns before a quiz
STUDENT_MAX_SESSION_TURNS = 20 # Total user turns before session ends (approx 30 mins)

# --- PDF Processing & Helpers (Mostly unchanged, minor robustness) ---
def split_sections(pdf_file_obj):
    if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0)
    # ... (split_sections logic remains largely the same as your last full version) ...
    # (Ensure it handles fitz and PyPDF2 fallback correctly)
    if fitz_available:
        try:
            doc = None
            if hasattr(pdf_file_obj, "name"): doc = fitz.open(pdf_file_obj.name)
            elif hasattr(pdf_file_obj, "read"):
                pdf_bytes_sec = pdf_file_obj.read(); pdf_file_obj.seek(0)
                doc = fitz.open(stream=pdf_bytes_sec, filetype="pdf")
            if not doc: raise Exception("Could not open PDF with fitz.")
            pages_text = [page.get_text("text", sort=True) for page in doc]; doc.close()
            headings = []
            for i, text in enumerate(pages_text):
                for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad|Part|Module)\s+[\d\w]+.*", text):
                    headings.append({"page": i + 1, "start_char_index": m.start(), "title": m.group().strip(), "page_index": i})
            headings.sort(key=lambda h: (h['page_index'], h['start_char_index']))
            sections = []
            if not headings:
                full_content = "\n".join(pages_text)
                if full_content.strip(): sections.append({'title': 'Full Document Content', 'content': full_content.strip(), 'page': 1})
                return sections
            for idx, h in enumerate(headings):
                current_page_idx, start_char = h['page_index'], h['start_char_index']; content = ''
                if idx + 1 < len(headings):
                    next_h = headings[idx+1]; next_page_idx, end_char = next_h['page_index'], next_h['start_char_index']
                    if current_page_idx == next_page_idx: content += pages_text[current_page_idx][start_char:end_char]
                    else:
                        content += pages_text[current_page_idx][start_char:] + '\n'
                        for p_idx in range(current_page_idx + 1, next_page_idx): content += pages_text[p_idx] + '\n'
                        content += pages_text[next_page_idx][:end_char]
                else:
                    content += pages_text[current_page_idx][start_char:] + '\n'
                    for p_idx in range(current_page_idx + 1, len(pages_text)): content += pages_text[p_idx] + '\n'
                if content.strip(): sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
            sections = [s for s in sections if len(s['content']) > len(s['title']) + 20]
            return sections
        except Exception as e_fitz: print(f"Error fitz splitting: {e_fitz}. Fallback.");
    try:
        from PyPDF2 import PdfReader
        if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0)
        reader = PdfReader(pdf_file_obj.name if hasattr(pdf_file_obj, "name") else pdf_file_obj)
        text = "\n".join(page.extract_text() or '' for page in reader.pages)
        chunks, sections, sents_per_sec = re.split(r'(?<=[.?!])\s+', text), [], 15
        for i in range(0, len(chunks), sents_per_sec):
            title, content = f"Content Block {i//sents_per_sec+1}", " ".join(chunks[i:i+sents_per_sec]).strip()
            if content: sections.append({'title': title, 'content': content, 'page': None})
        if not sections and text.strip(): sections.append({'title': 'Full Document (PyPDF2)', 'content': text.strip(), 'page': None})
        return sections
    except ImportError: return [{'title': 'PDF Error', 'content': 'PyPDF2 not found.', 'page': None}]
    except Exception as e_pypdf2: return [{'title': 'PDF Error', 'content': f'{e_pypdf2}', 'page': None}]

def download_docx(content, filename):
    # ... (download_docx logic remains the same) ...
    buf = io.BytesIO(); doc = Document()
    for line in content.split("\n"):
        p = doc.add_paragraph()
        parts = re.split(r'(\*\*.*?\*\*)', line)
        for part in parts:
            if part.startswith('**') and part.endswith('**'): p.add_run(part[2:-2]).bold = True
            else: p.add_run(part)
    doc.save(buf); buf.seek(0); return buf, filename

def count_classes(sd, ed, wdays):
    # ... (count_classes logic remains the same) ...
    cnt, cur = 0, sd
    while cur <= ed:
        if cur.weekday() in wdays: cnt += 1
        cur += timedelta(days=1)
    return cnt
    
def generate_access_token(student_id, course_id, lesson_id, lesson_date_obj):
    # ... (generate_access_token logic remains the same) ...
    if isinstance(lesson_date_obj, str): lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()
    iat = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    exp = iat + timedelta(hours=LINK_VALIDITY_HOURS)
    payload = {"sub": student_id, "course_id": course_id, "lesson_id": lesson_id, "iat": iat, "exp": exp, "aud": "https://www.easyaitutor.com"} # Assuming this is the intended audience
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)

def generate_5_digit_code(): return str(random.randint(10000, 99999))

def send_email_notification(to_email, subject, html_content, from_name="User", attachment_file_obj=None):
    # ... (send_email_notification logic with robust error handling and attachment, as developed) ...
    if not SMTP_USER or not SMTP_PASS: print(f"CRITICAL SMTP ERROR: SMTP_USER or SMTP_PASS not configured. Cannot send email to {to_email}."); return False
    msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = f"AI Tutor Panel <{SMTP_USER}>"; msg["To"] = to_email
    if to_email.lower() == SMTP_USER.lower() and "@" in from_name: msg.add_header('Reply-To', from_name)
    msg.add_alternative(html_content, subtype='html')
    if attachment_file_obj and hasattr(attachment_file_obj, "name") and attachment_file_obj.name:
        try:
            with open(attachment_file_obj.name, 'rb') as fp: file_data = fp.read()
            ctype, encoding = mimetypes.guess_type(attachment_file_obj.name)
            if ctype is None or encoding is not None: ctype = 'application/octet-stream'
            maintype, subtype_val = ctype.split('/', 1)
            msg.add_attachment(file_data,maintype=maintype,subtype=subtype_val,filename=os.path.basename(attachment_file_obj.name))
            print(f"Attachment {os.path.basename(attachment_file_obj.name)} prepared.")
        except FileNotFoundError: print(f"Error attaching: File not found at {attachment_file_obj.name}")
        except Exception as e_attach: print(f"Error processing attachment {attachment_file_obj.name}: {e_attach}")
    try:
        print(f"Attempting to send email to {to_email} via {SMTP_SERVER}:{SMTP_PORT} as {SMTP_USER}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as s: # Increased timeout
            s.set_debuglevel(0) # 0 for production, 1 for debug
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print(f"Email successfully sent to {to_email}"); return True
    except smtplib.SMTPAuthenticationError as e: print(f"SMTP Auth Error for {SMTP_USER}: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPConnectError as e: print(f"SMTP Connect Error to {SMTP_SERVER}:{SMTP_PORT}: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPServerDisconnected as e: print(f"SMTP Server Disconnected: {e}\n{traceback.format_exc()}"); return False
    except smtplib.SMTPException as e: print(f"General SMTP Exception to {to_email}: {e}\n{traceback.format_exc()}"); return False
    except Exception as e: print(f"Unexpected error sending email to {to_email}: {e}\n{traceback.format_exc()}"); return False

# --- Syllabus & Lesson Plan Generation (Instructor Panel) ---
def generate_syllabus(cfg):
    # ... (generate_syllabus logic remains the same) ...
    sd, ed = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date(), datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    mr, total = f"{sd.strftime('%B')}–{ed.strftime('%B')}", count_classes(sd, ed, [days_map[d] for d in cfg['class_days']])
    header = [f"Course: {cfg['course_name']}", f"Prof: {cfg['instructor']['name']}", f"Email: {cfg['instructor']['email']}", f"Duration: {mr} ({total} classes)", '_'*60]
    objectives = [f" • {o}" for o in cfg['learning_objectives']]
    body = ["DESC:", cfg['course_description'], "", "OBJECTIVES:"] + objectives + ["", "GRADING:", " • Quiz per class.", " • Retake if <60%.", " • Final = quiz avg.", "", "SCHEDULE:", f" • {mr}, {', '.join(cfg['class_days'])}", "", "SUPPORT:", " • Office Hours: Tue 3–5PM; Thu 10–11AM (Zoom)", " • Email reply <24h weekdays"]
    return "\n".join(header + [""] + body)

def generate_plan_by_week_structured_and_formatted(cfg):
    # ... (generate_plan_by_week_structured_and_formatted logic using character segmentation, as developed) ...
    sd, ed = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date(), datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    wdays = {days_map[d] for d in cfg['class_days']}
    class_dates = [cur for cur in (sd + timedelta(i) for i in range((ed - sd).days + 1)) if cur.weekday() in wdays]
    print(f"DEBUG: Class dates: {len(class_dates)}")
    if not class_dates: return "No class dates.", []
    full_text, char_map = cfg.get("full_text_content", ""), cfg.get("char_offset_page_map", [])
    if not full_text.strip():
        print("Warning: Full text empty."); 
        placeholder_lessons, placeholder_lines, weeks_ph = [], [], {}
        for idx, dt in enumerate(class_dates):
            wk_key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            ld = {"lesson_number": idx + 1, "date": dt.strftime('%Y-%m-%d'), "topic_summary": "Topic TBD (No PDF text)", "original_section_title": "N/A", "page_reference": None}
            placeholder_lessons.append(ld); weeks_ph.setdefault(wk_key, []).append(ld)
        for wk_key in sorted(weeks_ph.keys()):
            yr, wk = wk_key.split("-W"); placeholder_lines.append(f"**Week {wk} (Year {yr})**\n")
            for lsn in weeks_ph[wk_key]: placeholder_lines.append(f"**Lesson {lsn['lesson_number']} ({datetime.strptime(lsn['date'], '%Y-%m-%d').strftime('%B %d, %Y')})**: {lsn['topic_summary']}")
            placeholder_lines.append('')
        return "\n".join(placeholder_lines), placeholder_lessons

    total_chars, num_lessons = len(full_text), len(class_dates)
    chars_per_lesson = total_chars // num_lessons if num_lessons > 0 else total_chars
    min_chars, summaries, cur_ptr, seg_starts = 150, [], 0, []
    print(f"DEBUG: Total chars: {total_chars}, Chars/lesson: {chars_per_lesson}")
    for i in range(num_lessons):
        seg_starts.append(cur_ptr); start = cur_ptr
        end = cur_ptr + chars_per_lesson if i < num_lessons - 1 else total_chars
        seg_text, cur_ptr = full_text[start:end].strip(), end
        if len(seg_text) < min_chars: summaries.append("Review or brief topic.")
        else:
            try:
                print(f"DEBUG: Summarizing seg {i+1} (len {len(seg_text)}): '{seg_text[:70].replace(chr(10),' ')}...'")
                resp = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"system","content":"Identify core concept. Respond ONLY with short phrase (max 10-12 words) as lesson topic title, preferably gerund (e.g., 'Using verbs'). NO full sentences."}, {"role":"user","content": seg_text}], temperature=0.4, max_tokens=30)
                summaries.append(resp.choices[0].message.content.strip().replace('"', '').capitalize())
            except Exception as e: print(f"Error summarizing seg {i+1}: {e}"); summaries.append(f"Topic seg {i+1} (Summary Error)")
    
    lessons_by_course_week = {} # Use a different dictionary name
    structured_lessons = []

    if not class_dates: # Should have been caught earlier, but good to be safe
        return "No class dates to process.", []

    # Determine the ISO week and year of the very first class date
    first_class_date = class_dates[0]
    # To calculate course week, we find the Monday of the week for each class date
    # and count unique Mondays.
    
    course_week_counter = 0
    current_week_monday_for_grouping = None

    for idx, dt_obj in enumerate(class_dates): # dt_obj is the specific date of a class
        # Find the Monday of the current class date's week
        monday_of_this_week = dt_obj - timedelta(days=dt_obj.weekday())

        if current_week_monday_for_grouping is None or monday_of_this_week > current_week_monday_for_grouping:
            course_week_counter += 1
            current_week_monday_for_grouping = monday_of_this_week
        
        # Use the course_week_counter for grouping.
        # We also include the year of that Monday to handle courses spanning year-end.
        year_of_this_course_week = monday_of_this_week.year 
        course_week_key = f"{year_of_this_course_week}-CW{course_week_counter:02d}" # CW for Course Week

        # ... (logic to get summary_for_lesson, page_num, original_title_for_lesson, est_pg) ...
        # This part remains the same as your last full version
        summary_for_lesson = summaries[idx]
        est_pg = None
        if char_map:
            seg_start = seg_starts[idx]
            for offset, pg in reversed(char_map):
                if seg_start >= offset: est_pg = pg; break
            if est_pg is None and char_map: est_pg = char_map[0][1]
        
        lesson_data = {
            "lesson_number": idx + 1, 
            "date": dt_obj.strftime('%Y-%m-%d'),
            "topic_summary": summary_for_lesson, 
            "original_section_title": f"Text Segment {idx+1}", 
            "page_reference": est_pg 
        }
        # ... end of lesson_data creation ...

        structured_lessons.append(lesson_data)
        lessons_by_course_week.setdefault(course_week_key, []).append(lesson_data) # CORRECTED

    formatted_lines = []
    # Sort by the new course_week_key
    for course_week_key in sorted(lessons_by_course_week.keys()): # CORRECTED
        # Extract the course week number for display
        # The year is mostly for internal key uniqueness if course spans years
        year_disp, course_week_num_disp_str = course_week_key.split("-CW") 
        course_week_num_disp = int(course_week_num_disp_str) 

        # Get the start date of this course week for display context (optional but nice)
        first_date_in_this_week_group = lessons_by_course_week[course_week_key][0]['date'] # CORRECTED
        first_date_obj = datetime.strptime(first_date_in_this_week_group, '%Y-%m-%d')
        # Display the year of the actual classes in that week
        formatted_lines.append(f"**Course Week {course_week_num_disp} (Year {first_date_obj.year})**\n") 
        
        for lesson in lessons_by_course_week[course_week_key]: # CORRECTED
            ds = datetime.strptime(lesson['date'], '%Y-%m-%d').strftime('%B %d, %Y')
            pstr = f" (Approx. Ref. p. {lesson['page_reference']})" if lesson['page_reference'] else ''
            formatted_lines.append(f"**Lesson {lesson['lesson_number']} ({ds})**{pstr}: {lesson['topic_summary']}")
        formatted_lines.append('')
    return "\n".join(formatted_lines), structured_lessons

# ... (send_daily_class_reminders and check_student_progress_and_notify_professor functions as previously defined) ...
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
                        access_link = f"https://www.easyaitutor.com/class?token={token}" # Ensure your domain is correct
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

def log_student_progress(student_id, course_id, lesson_id, quiz_score_str, session_duration_secs, engagement_notes="N/A"):
    """Logs student progress to a CSV file."""
    # Ensure header exists
    if not PROGRESS_LOG_FILE.exists():
        with open(PROGRESS_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "student_id", "course_id", "lesson_id", "quiz_score", "session_duration_seconds", "engagement_notes"])
    
    with open(PROGRESS_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(dt_timezone.utc).isoformat(), student_id, course_id, lesson_id, quiz_score_str, session_duration_secs, engagement_notes])
    print(f"Progress logged for student {student_id}, lesson {lesson_id} of course {course_id}.")


def check_student_progress_and_notify_professor():
    print(f"SCHEDULER: Running student progress check at {datetime.now(dt_timezone.utc)}")
    # This function will now read from PROGRESS_LOG_FILE
    # For simplicity, it checks for any recent low scores.
    # A more robust system would track per student, per lesson, and avoid re-notifying.
    
    if not PROGRESS_LOG_FILE.exists():
        print("SCHEDULER: Progress log file does not exist. Skipping check.")
        return

    one_day_ago = datetime.now(dt_timezone.utc) - timedelta(days=1)
    alerts_to_send = {} # course_id -> {student_id: [messages]}

    try:
        with open(PROGRESS_LOG_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    log_timestamp = datetime.fromisoformat(row["timestamp"])
                    if log_timestamp < one_day_ago: # Only check recent logs (e.g., last 24 hours)
                        continue

                    quiz_score_str = row.get("quiz_score", "0/0") # e.g., "1/2"
                    if "/" in quiz_score_str:
                        correct, total_qs = map(int, quiz_score_str.split('/'))
                        if total_qs > 0 and (correct / total_qs) < 0.60:
                            student_id = row["student_id"]
                            course_id = row["course_id"]
                            lesson_id = row["lesson_id"]
                            
                            # Prepare alert message
                            alert_msg = (f"Student {student_id} scored {quiz_score_str} "
                                         f"on lesson {lesson_id} (logged {log_timestamp.strftime('%Y-%m-%d %H:%M')} UTC). "
                                         f"Session duration: {row.get('session_duration_seconds','N/A')}s. "
                                         f"Notes: {row.get('engagement_notes','N/A')}")
                            
                            alerts_to_send.setdefault(course_id, {}).setdefault(student_id, []).append(alert_msg)
                except ValueError:
                    print(f"SCHEDULER: Skipping malformed row in progress log: {row}")
                    continue
    except Exception as e_read_log:
        print(f"SCHEDULER: Error reading progress log: {e_read_log}")
        return

    # Send alerts
    for course_id, student_alerts in alerts_to_send.items():
        config_path = CONFIG_DIR / f"{course_id}_config.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                instructor_email = cfg.get("instructor", {}).get("email")
                instructor_name = cfg.get("instructor", {}).get("name", "Instructor")
                course_name = cfg.get("course_name", course_id)

                if instructor_email:
                    for student_id, messages in student_alerts.items():
                        subject = f"Student Progress Alert: {student_id} in {course_name}"
                        alerts_html = "<ul>" + "".join([f"<li>{msg}</li>" for msg in messages]) + "</ul>"
                        body_html = (f"<html><body><p>Dear {instructor_name},</p>"
                                     f"<p>One or more students in your course '{course_name}' may require attention based on recent AI Tutor sessions:</p>"
                                     f"{alerts_html}"
                                     f"<p>Please consider reviewing their progress and engaging with them directly.</p>"
                                     f"<p>Best regards,<br>AI Tutor Monitoring System</p></body></html>")
                        send_email_notification(instructor_email, subject, body_html, "AI Tutor System")
                        print(f"SCHEDULER: Sent progress alert for student {student_id} in course {course_name} to {instructor_email}")
            except Exception as e_send_alert:
                print(f"SCHEDULER: Error sending progress alert for course {course_id}: {e_send_alert}")


# --- Gradio Callbacks (Instructor Panel) ---
# ... (_get_syllabus_text_from_config, _get_plan_text_from_config, enable_edit_syllabus_and_reload, enable_edit_plan_and_reload as before) ...
def _get_syllabus_text_from_config(course_name_str):
    if not course_name_str: return "Error: Course name missing."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try: return generate_syllabus(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e: return f"Error loading syllabus: {e}"

def _get_plan_text_from_config(course_name_str):
    if not course_name_str: return "Error: Course name missing."
    path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
    if not path.exists(): return f"Error: Config for '{course_name_str}' not found."
    try: return json.loads(path.read_text(encoding="utf-8")).get("lesson_plan_formatted", "Plan not generated.")
    except Exception as e: return f"Error loading plan: {e}"

def enable_edit_syllabus_and_reload(current_course_name, current_output_content):
    if not current_output_content.strip().startswith("Course:"): 
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
    # ... (save_setup logic as previously defined, ensuring it saves full_text_content and char_offset_page_map) ...
    num_expected_outputs = 13 
    def error_return_tuple(error_message_str):
        return (gr.update(value=error_message_str, visible=True, interactive=False), gr.update(visible=True), None, gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), gr.update(value="", visible=False), gr.update(visible=True), gr.update(visible=False))
    try:
        if not all([course_name, instr_name, instr_email, pdf_file, sy, sm, sd_day, ey, em, ed_day, class_days_selected]): return error_return_tuple("⚠️ Error: All fields marked with * are required.")
        try:
            start_dt, end_dt = datetime(int(sy), int(sm), int(sd_day)), datetime(int(ey), int(em), int(ed_day))
            if end_dt <= start_dt: return error_return_tuple("⚠️ Error: End date must be after start date.")
        except ValueError: return error_return_tuple("⚠️ Error: Invalid date selected.")

        sections_for_desc_obj = split_sections(pdf_file)
        if not sections_for_desc_obj or (len(sections_for_desc_obj) == 1 and "Error" in sections_for_desc_obj[0]['title']):
             return error_return_tuple("⚠️ Error: Could not extract structural sections from PDF for analysis.")

        full_pdf_text, char_offset_to_page_map, current_char_offset = "", [], 0
        fitz_available_for_full_text = fitz_available 
        if fitz_available_for_full_text:
            doc_for_full_text = None
            try:
                if hasattr(pdf_file, "seek"): pdf_file.seek(0)
                if hasattr(pdf_file, "name"): doc_for_full_text = fitz.open(pdf_file.name)
                elif hasattr(pdf_file, "read"):
                    pdf_bytes = pdf_file.read(); pdf_file.seek(0)
                    doc_for_full_text = fitz.open(stream=pdf_bytes, filetype="pdf")
                if doc_for_full_text:
                    for page_num_fitz, page_obj in enumerate(doc_for_full_text):
                        page_text = page_obj.get_text("text", sort=True) 
                        if page_text: char_offset_to_page_map.append((current_char_offset, page_num_fitz + 1)); full_pdf_text += page_text + "\n"; current_char_offset += len(page_text) + 1
                    doc_for_full_text.close()
                else: fitz_available_for_full_text = False 
            except Exception as e_fitz_full: print(f"Error extracting full text with fitz: {e_fitz_full}"); fitz_available_for_full_text = False
        
        if not fitz_available_for_full_text or not full_pdf_text.strip(): 
            print("Warning: Fitz failed or not used for full text extraction, using concatenated sections. Page map will be empty or less accurate.")
            if hasattr(pdf_file, "seek"): pdf_file.seek(0) 
            temp_sections = split_sections(pdf_file) 
            full_pdf_text = "\n".join(s['content'] for s in temp_sections); char_offset_to_page_map = []
        
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
        return (gr.update(value=syllabus_text, visible=True, interactive=False), gr.update(visible=False), None, gr.update(visible=True), gr.update(visible=True), gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), gr.update(visible=True), gr.update(value="", visible=False), gr.update(visible=False), gr.update(visible=True, value=course_name))
    except openai.APIError as oai_err: print(f"OpenAI Error: {oai_err}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ OpenAI API Error: {oai_err}.")
    except Exception as e: print(f"Error in save_setup: {e}\n{traceback.format_exc()}"); return error_return_tuple(f"⚠️ Error: {e}")

def generate_plan_callback(course_name_from_input):
    # ... (generate_plan_callback logic as previously defined, including the notification message) ...
    def error_return_for_plan(error_message_str):
        return (gr.update(value=error_message_str, visible=True, interactive=False), None, None, gr.update(visible=True), None, None, gr.update(visible=False), gr.update(visible=False))
    try:
        if not course_name_from_input: return error_return_for_plan("⚠️ Error: Course Name required.")
        path = CONFIG_DIR / f"{course_name_from_input.replace(' ','_').lower()}_config.json"
        if not path.exists(): return error_return_for_plan(f"⚠️ Error: Config for '{course_name_from_input}' not found.")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg)
        
        cfg["lessons"] = structured_lessons_list 
        cfg["lesson_plan_formatted"] = formatted_plan_str
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        class_days_str = ", ".join(cfg.get("class_days", ["configured schedule"])) 
        notification_message = (f"\n\n---\n✅ **Lesson Plan Generated & Email System Activated for Class Days!**\n"
            f"Students in this course will now receive emails with a unique link "
            f"to their AI Tutor lesson on each scheduled class day ({class_days_str}). "
            f"Links are active from 6 AM to 12 PM UTC on those days.")
        display_text_for_plan_box = formatted_plan_str + notification_message
        
        return (gr.update(value=display_text_for_plan_box, visible=True, interactive=False), None, None, gr.update(visible=False), None, None, gr.update(visible=True), gr.update(visible=True)) 
    except openai.APIError as oai_err: print(f"OpenAI Error: {oai_err}\n{traceback.format_exc()}"); return error_return_for_plan(f"⚠️ OpenAI API Error: {oai_err}.")
    except Exception as e: print(f"Error in generate_plan_callback: {e}\n{traceback.format_exc()}"); return error_return_for_plan(f"⚠️ Error: {e}")

def email_document_callback(course_name, doc_type, output_text_content, students_input_str):
    # ... (email_document_callback logic as previously defined, with refined SMTP error handling) ...
    if not SMTP_USER or not SMTP_PASS: return gr.update(value="⚠️ Error: SMTP settings not configured.")
    try:
        if not course_name or not output_text_content: return gr.update(value=f"⚠️ Error: Course Name & {doc_type} content required.")
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        if not path.exists(): return gr.update(value=f"⚠️ Error: Config for '{course_name}' not found.")
        cfg = json.loads(path.read_text(encoding="utf-8")); instr_name, instr_email = cfg.get("instructor", {}).get("name", "Instructor"), cfg.get("instructor", {}).get("email")
        buf, fn = download_docx(output_text_content, f"{course_name.replace(' ','_')}_{doc_type.lower()}.docx"); attachment_data = buf.read()
        recipients = ([{"name":instr_name, "email":instr_email}] if instr_email else []) + [{"name":n.strip(), "email":e.strip()} for ln in students_input_str.splitlines() if ',' in ln for n,e in [ln.split(',',1)]]
        if not recipients: return gr.update(value="⚠️ Error: No recipients.")
        s_count, errs = 0, []
        for rec in recipients:
            msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = f"{doc_type.capitalize()}: {course_name}", SMTP_USER, rec["email"]
            msg.set_content(f"Hi {rec['name']},\n\nAttached is {doc_type.lower()} for {course_name}.\n\nBest,\nAI Tutor System"); msg.add_attachment(attachment_data, maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename=fn)
            try: 
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as s: s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg) # Added timeout
                s_count+=1
            except smtplib.SMTPRecipientsRefused as e_recp:
                err_str = str(e_recp).lower(); keywords = ["not a valid rfc", "address rejected", "user unknown", "no such user", "bad recipient", "invalid mailbox"]
                is_invalid = any(k in err_str for k in keywords)
                err_msg = f"Error for {rec['email']}: Please ensure this is a valid email address." if is_invalid else f"SMTP Err (Recipient) for {rec['email']}: {e_recp}"
                print(f"SMTP Recipient Refused for {rec['email']}: {e_recp}"); errs.append(err_msg)
            except smtplib.SMTPAuthenticationError as e_auth: errs.append(f"SMTP Auth Err (for {rec['email']}): Check sender credentials.")
            except Exception as e_smtp: errs.append(f"SMTP Err for {rec['email']}: {e_smtp}")
        status = f"✅ {doc_type.capitalize()} sent to {s_count} recipient(s)."; status += f"\n⚠️ Errors:\n" + "\n".join(errs) if errs else ""
        return gr.update(value=status)
    except Exception as e: err_txt = f"⚠️ Emailing Err:\n{traceback.format_exc()}"; print(err_txt); return gr.update(value=err_txt)

def email_syllabus_callback(c, s_str, out_content): return email_document_callback(c, "Syllabus", out_content, s_str)
def email_plan_callback(c, s_str, out_content): return email_document_callback(c, "Lesson Plan", out_content, s_str)

# --- Build Instructor UI ---
def build_instructor_ui():
    import time  # ensure time is in scope for the send placeholder
    with gr.Blocks(theme=gr.themes.Soft()) as instructor_demo:
        gr.Markdown("## AI Tutor Instructor Panel")
        with gr.Tabs():
            # --- Tab 1: Course Setup & Syllabus ---
            with gr.TabItem("Course Setup & Syllabus"):
                with gr.Row():
                    course = gr.Textbox(label="Course Name*")
                    instr = gr.Textbox(label="Instructor Name*")
                    email = gr.Textbox(label="Instructor Email*", type="email")
                pdf_file = gr.File(label="Upload Course Material PDF*", file_types=[".pdf"])
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("#### Course Schedule")
                        years      = [str(y) for y in range(datetime.now().year, datetime.now().year + 5)]
                        months     = [f"{m:02d}" for m in range(1, 13)]
                        days_list  = [f"{d:02d}" for d in range(1, 32)]
                        with gr.Row():
                            sy       = gr.Dropdown(years, label="Start Year*")
                            sm       = gr.Dropdown(months, label="Start Month*")
                            sd_day   = gr.Dropdown(days_list, label="Start Day*")
                        with gr.Row():
                            ey       = gr.Dropdown(years, label="End Year*")
                            em       = gr.Dropdown(months, label="End Month*")
                            ed_day   = gr.Dropdown(days_list, label="End Day*")
                        class_days_selected = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*")
                    with gr.Column(scale=1):
                        gr.Markdown("#### Student & Access")
                        devices            = gr.CheckboxGroup(["Phone", "PC", "Tablet"], label="Allowed Devices", value=["PC"])
                        students_input_str = gr.Textbox(
                            label="Students (Name,Email per line)",
                            lines=5,
                            placeholder="S. One,s1@ex.com\nS. Two,s2@ex.com"
                        )
                btn_save  = gr.Button("1. Save Setup & Generate Syllabus", variant="primary")
                gr.Markdown("---")
                output_box = gr.Textbox(
                    label="Output", lines=20, interactive=False, visible=False, show_copy_button=True
                )
                with gr.Row(visible=False) as syllabus_actions_row:
                    btn_edit_syl  = gr.Button(value="📝 Edit Syllabus Text")
                    btn_email_syl = gr.Button(value="📧 Email Syllabus", variant="secondary")

            # --- Tab 2: Lesson Plan Management ---
            with gr.TabItem("Lesson Plan Management"):
                lesson_plan_setup_message = gr.Markdown(
                    value=(
                        "### Course Setup Required\n"
                        "Course Setup (on Tab 1) must be completed before generating a Lesson Plan."
                    ),
                    visible=True
                )
                course_load_for_plan = gr.Textbox(
                    label="Course Name for Lesson Plan",
                    placeholder="e.g., Introduction to Python",
                    visible=False
                )
                output_plan_box = gr.Textbox(
                    label="Lesson Plan Output",
                    lines=20, interactive=False, visible=False, show_copy_button=True
                )
                with gr.Row(visible=False) as plan_buttons_row:
                    btn_generate_plan = gr.Button("2. Generate/Re-generate Lesson Plan", variant="primary")
                    btn_edit_plan     = gr.Button(value="📝 Edit Plan Text")
                    btn_email_plan    = gr.Button(value="📧 Email Lesson Plan", variant="secondary")

            
            # --- Tab 3: Contact Support ---
            with gr.TabItem("Contact Support"):
                gr.Markdown("### Send a Message to Support")
                with gr.Row():
                    contact_name       = gr.Textbox(label="Your Name")
                    contact_email_addr = gr.Textbox(label="Your Email Address")
                contact_message    = gr.Textbox(
                    label="Message",
                    lines=5,
                    placeholder="Type your message here..."
                )
                contact_attachment = gr.File(label="Attach File (Optional)", file_count="single")
                btn_send_contact_email = gr.Button("Send Message", variant="primary")
                contact_status_output  = gr.Markdown(value="") # For status messages

                # --- Contact Support callback DEFINITION ---
                # Note: Parameter names are changed slightly to avoid potential confusion
                # with the component variables themselves, though in Gradio callbacks,
                # they are passed by value.
                def handle_contact_submission(name, email, message, attachment):
                    errors = []
                    if not name.strip():          errors.append("Name is required.")
                    if not email.strip():         errors.append("Email is required.")
                    elif "@" not in email:        errors.append("Enter a valid email.")
                    if not message.strip():       errors.append("Message is required.")
                    if errors:
                        return (
                            gr.update(value="Please fix:\n" + "\n".join(f"- {e}" for e in errors)),
                            gr.update(value=name),
                            gr.update(value=email),
                            gr.update(value=message),
                            gr.update(value=attachment)
                        )
                
                    # no more yield here
                    sent = send_email_notification("easyaitutor@gmail.com",
                                                   f"Contact: {name} <{email}>",
                                                   message.replace("\n","<br>"),
                                                   from_name=email,
                                                   attachment_file_obj=attachment)
                
                    if sent:
                        return (
                            gr.update(value="<span style='color:green;'>Sent! ✔</span>"),
                            gr.update(value=""),
                            gr.update(value=""),
                            gr.update(value=""),
                            gr.update(value=None)
                        )
                    else:
                        return (
                            gr.update(value="<span style='color:red;'>Failed to send. Check SMTP.</span>"),
                            gr.update(value=name),
                            gr.update(value=email),
                            gr.update(value=message),
                            gr.update(value=attachment)
                        )
                
                btn_send_contact_email.click(
                    handle_contact_submission,
                    inputs=[contact_name, contact_email_addr, contact_message, contact_attachment],
                    outputs=[contact_status_output, contact_name, contact_email_addr, contact_message, contact_attachment],
                    queue=True
                )
            # --- END OF TabItem("Contact Support") ---

        # ... (dummy buttons and other .click() registrations for Tab 1 & 2) ...
        # These should be at the indentation level of `with gr.Tabs():` or `with gr.Blocks():`
        # depending on where they are defined in your full structure.
        # Based on your snippet, they are likely direct children of `with gr.Blocks()`.
        dummy_btn_1 = gr.Button(visible=False)
        dummy_btn_2 = gr.Button(visible=False)
        dummy_btn_3 = gr.Button(visible=False)
        dummy_btn_4 = gr.Button(visible=False)

        # Hook up all the buttons to their callbacks
        # These are also likely direct children of `with gr.Blocks()`.
        btn_save.click(
            save_setup,
            inputs=[
                course, instr, email, devices, pdf_file,
                sy, sm, sd_day, ey, em, ed_day,
                class_days_selected, students_input_str
            ],
            outputs=[
                output_box, btn_save, dummy_btn_1, btn_generate_plan,
                btn_edit_syl, btn_email_syl, btn_edit_plan,
                btn_email_plan, syllabus_actions_row, plan_buttons_row,
                output_plan_box, lesson_plan_setup_message,
                course_load_for_plan
            ],
        )
        btn_edit_syl.click(
            enable_edit_syllabus_and_reload,
            inputs=[course, output_box],
            outputs=[output_box]
        )
        btn_email_syl.click(
            email_syllabus_callback,
            inputs=[course, students_input_str, output_box],
            outputs=[output_box]
        )
        btn_generate_plan.click(
            generate_plan_callback,
            inputs=[course_load_for_plan],
            outputs=[
                output_plan_box, dummy_btn_2, dummy_btn_1,
                btn_generate_plan, dummy_btn_3, dummy_btn_4,
                btn_edit_plan, btn_email_plan
            ]
        ).then(
            lambda: (gr.update(visible=True), gr.update(visible=True)),
            outputs=[output_plan_box, plan_buttons_row]
        )
        btn_edit_plan.click(
            enable_edit_plan_and_reload,
            inputs=[course_load_for_plan, output_plan_box],
            outputs=[output_plan_box]
        )
        btn_email_plan.click(
            email_plan_callback,
            inputs=[course_load_for_plan, students_input_str, output_plan_box],
            outputs=[output_plan_box]
        )
        course.change(
            lambda x: x,
            inputs=[course],
            outputs=[course_load_for_plan]
        )
    # This return is for the build_instructor_ui function
    return instructor_demo

 
# --- Student Tutor UI and Logic ---
# This will be a new section, adapted from your student tutor script
# For simplicity, we'll make it a function that returns a Gradio Blocks instance too.

def build_student_tutor_ui(course_id: str, lesson_id: int, student_id: str, lesson_topic: str, lesson_segment_text: str):
    """
    Builds the Gradio UI for the student's interactive tutoring session.
    This UI is initialized with specific lesson context.
    """
    # Initialize OpenAI client for student tutor (if not already global, or pass it)
    # client = openai.OpenAI() # Assuming client is already globally defined and configured

    # --- Student Tutor Configuration (copied from your script for context) ---
    # TTS_MODEL = "tts-1"
    # CHAT_MODEL = "gpt-4o-mini" # Or gpt-3.5-turbo
    # WHISPER_MODEL = "whisper-1"
    # DEFAULT_ENGLISH_LEVEL = "B1 (Intermediate)" # This could also come from student profile later
    # AUDIO_DIR = Path("student_audio_files") # Use STUDENT_AUDIO_DIR
    # BOT_NAME = "Easy AI Tutor" # Use STUDENT_BOT_NAME
    # LOGO_PATH = "logo.png" # Use STUDENT_LOGO_PATH

    # ONBOARDING_TURNS = 2
    # TEACHING_TURNS_PER_BREAK = 5
    # INTEREST_BREAK_TURNS = 1
    # QUIZ_AFTER_TURNS = 7 
    # MAX_SESSION_TURNS = 20
    
    # Simplified system prompt generation for Phase 1
    def generate_student_system_prompt(mode, student_interests_str, current_topic, current_segment):
        base = f"You are {STUDENT_BOT_NAME}, a friendly AI English tutor. Your student's English level is {STUDENT_DEFAULT_ENGLISH_LEVEL}. Keep responses concise."
        if mode == "initial_greeting":
            return f"{base} Today's lesson is about: '{current_topic}'. Let's start by getting to know you. What are your hobbies?"
        elif mode == "onboarding":
            return f"{base} You are getting to know the student. Interests so far: {student_interests_str}. Ask another open-ended question about their interests."
        elif mode == "teaching_transition":
            return f"{base} Interests: {student_interests_str}. Smoothly transition to teaching about '{current_topic}' based on this text: \"{current_segment[:300]}...\" Ask an opening question related to it."
        elif mode == "teaching":
            return f"{base} You are teaching about '{current_topic}' using the text: \"{current_segment[:300]}...\". Interests for context: {student_interests_str}. Provide gentle corrections. End with a question."
        elif mode == "interest_break_transition":
            return f"{base} Time for a short break! Based on interests: {student_interests_str}, ask a light question or share a fun fact related to their interests."
        elif mode == "interest_break_active":
            return f"{base} Student responded to your interest break. Give a brief, engaging reply. Then you'll go back to teaching '{current_topic}'."
        elif mode == "quiz_time":
            return f"{base} It's quiz time for '{current_topic}'. Based on the text: \"{current_segment[:300]}...\", generate one multiple-choice question with 3 options (A, B, C) and clearly indicate the correct answer in your internal thought process but not to the student. Ask the question."
        elif mode == "ending_session":
            return f"{base} The session is ending. Briefly summarize or thank the student."
        return base # Default

    with gr.Blocks(theme=gr.themes.Soft()) as student_demo:
        gr.Markdown(f"# {STUDENT_BOT_NAME} - Lesson: {lesson_topic}")
        gr.Markdown(f"Course ID: {course_id}, Lesson ID: {lesson_id}, Student ID: {student_id}") # For debug

        # State variables for the student session
        st_chat_history = gr.State([]) # For LLM context
        st_display_history = gr.State([]) # For chatbot UI
        st_student_profile = gr.State({"interests": [], "quiz_score": {"correct": 0, "total": 0}})
        st_session_mode = gr.State("initial_greeting") # initial_greeting, onboarding, teaching, interest_break, quiz, ending
        st_turn_count = gr.State(0) # User turns
        st_teaching_turns_count = gr.State(0) # Teaching turns since last break/quiz
        st_session_start_time = gr.State(datetime.now(dt_timezone.utc))


        with gr.Row():
            with gr.Column(scale=1):
                st_voice_dropdown = gr.Dropdown(choices=["nova", "shimmer", "alloy"], value="nova", label="Tutor Voice")
                st_mic_input = gr.Audio(sources=["microphone"], type="filepath", label="Record response:")
                st_text_input = gr.Textbox(label="Or type response:", placeholder="Type here...")
                st_send_button = gr.Button("Send", variant="primary")
            with gr.Column(scale=3):
                st_chatbot = gr.Chatbot(label=f"Conversation with {STUDENT_BOT_NAME}", height=500)
                st_audio_out = gr.Audio(type="filepath", autoplay=False, label=f"{STUDENT_BOT_NAME} says:")

        def st_initial_load():
            # This function is called when the UI loads for the student.
            # The tutor makes the first statement.
            system_prompt = generate_student_system_prompt("initial_greeting", "", lesson_topic, lesson_segment_text)
            try:
                client = openai.OpenAI() # Ensure client is initialized
                llm_response = client.chat.completions.create(
                    model=STUDENT_CHAT_MODEL,
                    messages=[{"role": "system", "content": system_prompt}],
                    max_tokens=150
                )
                initial_tutor_message = llm_response.choices[0].message.content.strip()
            except Exception as e:
                print(f"STUDENT_TUTOR: OpenAI initial call failed: {e}")
                initial_tutor_message = f"Hello! Welcome. We'll be discussing '{lesson_topic}'. Unfortunately, I had a slight hiccup starting up. Let's try our best! What are your hobbies?"

            new_chat_hist = [{"role": "assistant", "content": initial_tutor_message}]
            new_display_hist = [[None, initial_tutor_message]]
            
            audio_fp_update = None
            try:
                client = openai.OpenAI()
                tts_resp = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice="nova", input=initial_tutor_message)
                intro_fp = STUDENT_AUDIO_DIR / f"intro_{uuid.uuid4()}.mp3"
                with open(intro_fp, "wb") as f: f.write(tts_resp.content)
                audio_fp_update = gr.update(value=str(intro_fp), autoplay=True)
            except Exception as e_tts:
                print(f"STUDENT_TUTOR: TTS for initial message failed: {e_tts}")
            
            return new_display_hist, new_chat_hist, "onboarding", 0, 0, audio_fp_update, datetime.now(dt_timezone.utc)


        def st_process_turn(mic_audio, typed_text, 
                            chat_h, display_h, profile, mode, turns, teaching_turns, voice, # States
                            s_id, c_id, l_id, topic, segment_text): # Fixed lesson context
            
            user_input_text = ""
            if mic_audio:
                try:
                    client = openai.OpenAI()
                    with open(mic_audio, "rb") as af:
                        transcription = client.audio.transcriptions.create(file=af, model=STUDENT_WHISPER_MODEL)
                    user_input_text = transcription.text.strip()
                    if not user_input_text: user_input_text = "(No speech detected)"
                except Exception as e: user_input_text = f"(Transcription error: {e})"
                try: os.remove(mic_audio)
                except: pass
            elif typed_text:
                user_input_text = typed_text.strip()
            else: # Should not happen if triggered by input change/submit
                return display_h, chat_h, profile, mode, turns, teaching_turns, gr.update(), gr.update(value=None), gr.update(value="")

            if not user_input_text: # Handle empty recognized speech
                 return display_h, chat_h, profile, mode, turns, teaching_turns, gr.update(), gr.update(value=None), gr.update(value="")


            display_h.append([user_input_text, None]) # Show user message immediately
            chat_h.append({"role": "user", "content": user_input_text})
            turns += 1

            # --- Mode and Turn Logic (Simplified for Phase 1) ---
            next_mode = mode
            if mode == "onboarding":
                profile["interests"].append(user_input_text) # Simple interest gathering
                if turns >= STUDENT_ONBOARDING_TURNS: next_mode = "teaching_transition"
            elif mode == "teaching_transition":
                next_mode = "teaching"
            elif mode == "teaching":
                teaching_turns += 1
                if teaching_turns % STUDENT_QUIZ_AFTER_TURNS == 0 and teaching_turns > 0 : # Quiz time
                    next_mode = "quiz_time"
                elif teaching_turns % STUDENT_TEACHING_TURNS_PER_BREAK == 0 and teaching_turns > 0: # Interest break
                    next_mode = "interest_break_transition"
            elif mode == "interest_break_transition":
                next_mode = "interest_break_active"
            elif mode == "interest_break_active":
                next_mode = "teaching" # Back to teaching
            elif mode == "quiz_time": # After student answers quiz
                # (Actual quiz answer evaluation would go here)
                # For now, just assume they answered and move back to teaching
                # We'll log a placeholder score later
                profile["quiz_score"]["total"] += 1 # Increment total questions
                # if answer_is_correct: profile["quiz_score"]["correct"] += 1
                next_mode = "teaching"


            if turns >= STUDENT_MAX_SESSION_TURNS:
                next_mode = "ending_session"

            # --- Generate Bot Response ---
            interests_str = ", ".join(profile["interests"]) if profile["interests"] else "not yet known"
            system_prompt = generate_student_system_prompt(next_mode, interests_str, topic, segment_text)
            
            bot_response_text = "I'm thinking..."
            try:
                client = openai.OpenAI()
                messages_for_llm = [{"role": "system", "content": system_prompt}] + chat_h
                llm_response = client.chat.completions.create(
                    model=STUDENT_CHAT_MODEL, messages=messages_for_llm, max_tokens=200
                )
                bot_response_text = llm_response.choices[0].message.content.strip()
            except Exception as e:
                print(f"STUDENT_TUTOR: OpenAI chat call failed: {e}")
                bot_response_text = "I had a little trouble processing that. Could you try rephrasing?"

            chat_h.append({"role": "assistant", "content": bot_response_text})
            display_h[-1][1] = bot_response_text # Update last entry in display history

            audio_fp_update = None
            try:
                client = openai.OpenAI()
                tts_resp = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice=voice, input=bot_response_text)
                reply_fp = STUDENT_AUDIO_DIR / f"reply_{uuid.uuid4()}.mp3"
                with open(reply_fp, "wb") as f: f.write(tts_resp.content)
                audio_fp_update = gr.update(value=str(reply_fp), autoplay=True)
            except Exception as e_tts:
                print(f"STUDENT_TUTOR: TTS for bot reply failed: {e_tts}")

            if next_mode == "ending_session":
                # Log progress here
                session_end_time = datetime.now(dt_timezone.utc)
                # session_duration = (session_end_time - st_session_start_time.value).total_seconds() # Needs st_session_start_time from state
                # For now, log placeholder duration
                log_student_progress(s_id, c_id, l_id, f"{profile['quiz_score']['correct']}/{profile['quiz_score']['total']}", 0)


            return display_h, chat_h, profile, next_mode, turns, teaching_turns, audio_fp_update, gr.update(value=None), gr.update(value="")

        # Load initial message from tutor
        student_demo.load(
            fn=st_initial_load, 
            outputs=[st_chatbot, st_chat_history, st_session_mode, st_turn_count, st_teaching_turns_count, st_audio_out, st_session_start_time]
        )

        # Event handlers for student input
        st_mic_input.change(
            fn=st_process_turn, 
            inputs=[st_mic_input, st_text_input, st_chat_history, st_display_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_voice_dropdown, 
                    gr.State(student_id), gr.State(course_id), gr.State(lesson_id), gr.State(lesson_topic), gr.State(lesson_segment_text)],
            outputs=[st_chatbot, st_chat_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_audio_out, st_mic_input, st_text_input],
            show_progress="hidden"
        )
        st_text_input.submit(
            fn=st_process_turn, 
            inputs=[st_mic_input, st_text_input, st_chat_history, st_display_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_voice_dropdown,
                    gr.State(student_id), gr.State(course_id), gr.State(lesson_id), gr.State(lesson_topic), gr.State(lesson_segment_text)],
            outputs=[st_chatbot, st_chat_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_audio_out, st_mic_input, st_text_input],
            show_progress="hidden"
        )
        st_send_button.click(
            fn=st_process_turn, 
            inputs=[st_mic_input, st_text_input, st_chat_history, st_display_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_voice_dropdown,
                    gr.State(student_id), gr.State(course_id), gr.State(lesson_id), gr.State(lesson_topic), gr.State(lesson_segment_text)],
            outputs=[st_chatbot, st_chat_history, st_student_profile, st_session_mode, st_turn_count, st_teaching_turns_count, st_audio_out, st_mic_input, st_text_input],
            show_progress="hidden"
        )
    return student_demo

# Templates for serving the initial HTML for the student tutor
templates = Jinja2Templates(directory="templates") # Create a 'templates' directory
# You might need to serve static files if your student UI has CSS/JS not handled by Gradio
# app.mount("/static_student", StaticFiles(directory="static_student"), name="static_student")


@app.get("/class", response_class=HTMLResponse)
async def get_student_lesson_page(request: Request, token: str = None):
    """
    Serves the initial HTML page that will then load the Gradio student tutor UI.
    Validates token and prepares lesson context.
    """
    if not token:
        return HTMLResponse("<h3>Error: Access token missing.</h3>", status_code=400)
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM], audience="https://www.easyaitutor.com")
        student_id = payload["sub"]
        course_id = payload["course_id"]
        lesson_id = int(payload["lesson_id"]) # Ensure it's an int
        # Expiration is checked by jwt.decode automatically

        # Load course config
        config_path = CONFIG_DIR / f"{course_id.replace(' ','_').lower()}_config.json"
        if not config_path.exists():
            raise HTTPException(status_code=404, detail="Course configuration not found.")
        
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        full_text = cfg.get("full_text_content", "")
        lessons_data = cfg.get("lessons", [])

        if not full_text or not lessons_data:
            raise HTTPException(status_code=404, detail="Lesson content or plan not found in configuration.")

        if not (0 < lesson_id <= len(lessons_data)):
             raise HTTPException(status_code=404, detail=f"Lesson ID {lesson_id} out of range.")

        # Get specific lesson topic
        lesson_topic = lessons_data[lesson_id - 1].get("topic_summary", f"Lesson {lesson_id}")

        # Calculate text segment for this lesson
        num_total_lessons = len(cfg.get("class_dates", lessons_data)) # Use class_dates if available, else len(lessons)
        if num_total_lessons == 0: num_total_lessons = 1 # Avoid division by zero
        
        chars_per_lesson = len(full_text) // num_total_lessons
        start_char = (lesson_id - 1) * chars_per_lesson
        end_char = lesson_id * chars_per_lesson if lesson_id < num_total_lessons else len(full_text)
        lesson_segment_text = full_text[start_char:end_char].strip()

        if not lesson_segment_text:
            lesson_segment_text = "(No specific text segment for this lesson, focusing on general topic review.)"
            print(f"Warning: Empty text segment for {course_id}, lesson {lesson_id}")


        # This is where you would normally render an HTML page that then loads the Gradio JS
        # For simplicity with Gradio, we'll mount another Gradio app specifically for the student.
        # This requires careful handling if many students access it.
        # A more scalable way is a single student Gradio app whose state is initialized.
        
        # We will pass these to the Gradio app for the student.
        # This is a simplified way; ideally, the Gradio app at /student_tutor_gradio_app
        # would itself fetch these based on the token via an API or shared context.
        # For now, we'll pass them conceptually.
        
        # The actual Gradio app for the student will be mounted separately.
        # This HTML response just needs to redirect or embed the Gradio app.
        # Simplest for now: redirect to the Gradio app path with parameters.
        # However, Gradio doesn't easily take startup parameters via URL for gr.Blocks.
        # So, the student Gradio app will need to parse the token from its own request context.

        # For now, let's just return a simple HTML page that tells the user they are being redirected
        # or that the lesson is loading. The actual Gradio app will be at /student_tutor_interface
        
        # This HTML is just a placeholder. The real magic happens when Gradio JS loads for the student UI.
        # We need to ensure the student_tutor_ui can get the token.
        # One way: render the token into a JavaScript variable on an HTML page.
        # Then the Gradio JS for the student UI can pick it up.
        
        # Create a simple HTML template (templates/student_view.html)
        # <!DOCTYPE html>
        # <html><head><title>Easy AI Tutor Lesson</title></head>
        # <body>
        #     <h1>Loading Your Lesson...</h1>
        #     <script>
        #         // This token will be used by the Gradio JS when it initializes the student UI
        #         window.lessonToken = "{{ token }}"; 
        #         // Redirect or load Gradio JS that points to the student Gradio app mount
        #         window.location.href = "/student_tutor_interface"; // Or directly embed Gradio JS
        #     </script>
        # </body></html>
        # For now, we will directly mount the student UI and it will have to get the token itself.
        # This is complex. A simpler Phase 1 might be to pass data via query params to a Gradio app
        # if Gradio's Blocks API supported easy init with params.

        # Let's assume the student Gradio app is mounted at /student_tutor_interface
        # and it will handle token extraction from its own request context when it loads.
        # This HTML is just a conceptual shell.
        return HTMLResponse(f"""
            <html>
                <head><title>Easy AI Tutor Lesson</title></head>
                <body>
                    <h2>Preparing your lesson for Course: {course_id}, Lesson: {lesson_id} ({lesson_topic})</h2>
                    <p>Student ID: {student_id}</p>
                    <p>If the lesson does not load automatically, please ensure JavaScript is enabled and refresh.</p>
                    <p>Token (for debug): {token}</p>
                    
                    {'''
                    <!-- This is where the Gradio app for the student would be embedded or linked -->
                    <!-- For a single-page app feel, the Gradio JS for the student UI would initialize here -->
                    <!-- For now, we will mount a separate Gradio app at /student_tutor_interface -->
                    <p><a href="/student_tutor_interface?token={token}">Click here to start your lesson if not redirected.</a></p>
                    <script>
                        // Optional: auto-redirect
                        // window.location.href = "/student_tutor_interface?token={token}";
                    </script>
                    '''}
                </body>
            </html>
        """)

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Access token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    except HTTPException as e: # Re-raise HTTPExceptions
        raise e
    except Exception as e:
        print(f"Error processing /class request: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error preparing lesson: {e}")

# --- Build and Mount Student Tutor UI ---
# This is tricky. We need to pass data to it.
# A global variable or a more complex setup is needed if we mount it directly.
# For Phase 1, the student UI will be simpler and might need to re-validate the token.

# Let's define a global to hold the student UI instance, to be created on first valid access.
# This is NOT ideal for concurrent users but simplifies initial integration.
# A better way is a factory function for the Gradio app.

# This is a simplified student UI for now, it will need to be enhanced
# to take the token and initialize itself.
# For now, it's a placeholder to show mounting.
# The actual student tutor logic from your script needs to be integrated here.

# We will create the student UI on demand when /student_tutor_interface is hit.
# This is still not ideal for Gradio's standard mounting.

# A better approach for dynamic student UI with Gradio:
# The /class endpoint serves a basic HTML page.
# This HTML page includes JavaScript that makes an API call (e.g., to /api/lesson_context?token=...)
# This API call returns the lesson_topic, lesson_segment_text.
# The JavaScript then initializes a Gradio *Interface* (not Blocks) dynamically, or
# updates components in a pre-loaded Gradio Blocks UI.

# For this iteration, let's make the student_tutor_ui a function that takes context
# and then try to mount it. This is still complex for gr.mount_gradio_app.

# The most straightforward way with gr.mount_gradio_app is to have a single student UI
# that then uses JavaScript to get the token from the URL and initialize its state.
# Gradio's Python `gr.State` is per-session, so this can work.

# Let's adapt the student tutor script to be a function that builds the UI,
# and its internal callbacks will need to access the token from the request if possible,
# or we pass it via gr.State initialized by a FastAPI route.

# This is where the student tutor UI from your script would be adapted and built.
# For now, this is a placeholder. The actual `build_student_tutor_ui`
# from the previous step needs to be fully integrated here.
# The challenge is passing the dynamic lesson_context to it effectively.

# --- FastAPI App Setup (Continued) ---
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

# The student tutor UI will be more complex to integrate directly here for dynamic content per link.
# A common pattern is to have the student UI make an API call with its token to get lesson data.
# For now, we will mount the instructor UI. The /class endpoint is a placeholder for how
# the student UI would be initiated.

# The student_tutor_ui_instance would be created by build_student_tutor_ui
# student_tutor_ui_instance = build_student_tutor_ui( DYNAMIC PARAMS NEEDED HERE )
# app = gr.mount_gradio_app(app, student_tutor_ui_instance, path="/student_tutor_interface")

if __name__ == "__main__":
    print("Starting App. Instructor Panel at /instructor. Student access via /class?token=...")
    # Note: The student UI part is not fully mounted as a dynamic Gradio app in this simplified __main__.
    # For full functionality, the /class endpoint needs to properly serve/initiate the student Gradio UI.
    # This usually involves more advanced FastAPI/Gradio integration patterns or a separate student app.
    
    # To run this: uvicorn main_script_name:app --reload --port 8000
    # Then access http://localhost:8000/instructor
    
    # For local testing of Gradio UI directly (without full FastAPI routing for /class)
    # instructor_ui_instance.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT",7860)))

    # The FastAPI app should be run with Uvicorn for both instructor and student parts to work via HTTP.
    import uvicorn
    uvicorn.run(
    app,
    host="127.0.0.1",               # ← bind only on localhost
    port=int(os.getenv("PORT", 8000)),
    reload=True                    # optional: enable auto-reload
)

    # Keep main thread alive for scheduler if not using uvicorn's lifecycle for it
    # This is generally handled by uvicorn when running the FastAPI app.
    # try:
    #     while True: time.sleep(2) 
    # except (KeyboardInterrupt, SystemExit):
    #     print("Shutting down scheduler (if running)...")
    #     if 'scheduler' in globals() and scheduler.running: 
    #         scheduler.shutdown()
