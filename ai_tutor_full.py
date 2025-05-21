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
# import time # Not strictly needed in the global scope now
import mimetypes
import csv

import openai # OpenAI client will be initialized within functions or globally if preferred later
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
# from fastapi.templating import Jinja2Templates # Not used in this revision
# from fastapi.staticfiles import StaticFiles # Not used in this revision

import jwt
# import requests # Not used in this revision
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

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "a-very-secure-secret-key-please-change")
if JWT_SECRET_KEY == "a-very-secure-secret-key-please-change":
    print("WARNING: JWT_SECRET_KEY is set to default. Please set a strong, unique secret key in your environment variables.")
LINK_VALIDITY_HOURS = 6
ALGORITHM = "HS256"
APP_DOMAIN = os.getenv("APP_DOMAIN", "https://www.easyaitutor.com") # For generating student links

# EASYAI_TUTOR_PROGRESS_API_ENDPOINT = os.getenv("EASYAI_TUTOR_PROGRESS_API_ENDPOINT") # Marked as less used

days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

# --- Student Tutor Configuration ---
STUDENT_TTS_MODEL = "tts-1"
STUDENT_CHAT_MODEL = "gpt-4o-mini"
STUDENT_WHISPER_MODEL = "whisper-1"
STUDENT_DEFAULT_ENGLISH_LEVEL = "B1 (Intermediate)"
STUDENT_AUDIO_DIR = Path("student_audio_files")
STUDENT_AUDIO_DIR.mkdir(exist_ok=True)
STUDENT_BOT_NAME = "Easy AI Tutor"
# STUDENT_LOGO_PATH = "logo.png" # Ensure this path is correct if used, or remove if not

STUDENT_ONBOARDING_TURNS = 2
STUDENT_TEACHING_TURNS_PER_BREAK = 5
STUDENT_INTEREST_BREAK_TURNS = 1
STUDENT_QUIZ_AFTER_TURNS = 7
STUDENT_MAX_SESSION_TURNS = 20

STUDENT_UI_PATH = "/student_tutor_interface" # Path for the student Gradio app

# Attempt to import fitz (PyMuPDF)
try:
    import fitz
    fitz_available = True
except ImportError:
    fitz_available = False
    print("PyMuPDF (fitz) not found. PDF processing quality may be reduced (using PyPDF2 fallback). Page number mapping will be limited.")

# ─── Create FastAPI app & CORS ───────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# --- APScheduler Setup ---
scheduler = BackgroundScheduler(timezone="UTC")

# --- Health‐check endpoint ---
@app.get("/healthz")
def healthz():
    return {"status": "ok", "scheduler_running": scheduler.running, "jobs": [job.id for job in scheduler.get_jobs()]}

# --- PDF Processing & Helpers (Assuming these are largely correct from previous versions) ---
def split_sections(pdf_file_obj):
    # This function remains complex. Assuming the logic from your previous full version.
    # For brevity, I'll use the placeholder, but ensure your full robust version is here.
    if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0)
    if fitz_available:
        try:
            doc = None
            if hasattr(pdf_file_obj, "name") and pdf_file_obj.name: # Ensure name is not None
                doc = fitz.open(pdf_file_obj.name)
            elif hasattr(pdf_file_obj, "read"):
                pdf_bytes_sec = pdf_file_obj.read()
                pdf_file_obj.seek(0) # Reset pointer after read
                doc = fitz.open(stream=pdf_bytes_sec, filetype="pdf")
            
            if not doc: raise Exception("Could not open PDF with fitz.")
            
            pages_text = [page.get_text("text", sort=True) for page in doc]
            doc.close()
            
            headings = [] # Example: find headings (highly dependent on PDF structure)
            for i, text_content in enumerate(pages_text):
                # Regex for headings (example, needs adjustment for diverse PDFs)
                for m in re.finditer(r"(?im)^(?:CHAPTER|Cap[ií]tulo|Sección|Section|Unit|Unidad|Part|Module)\s+[\d\w]+.*", text_content):
                    headings.append({"page": i + 1, "start_char_index_on_page": m.start(), "title": m.group().strip(), "page_index": i})
            
            headings.sort(key=lambda h: (h['page_index'], h['start_char_index_on_page']))
            
            sections = []
            if not headings: # Fallback: treat whole PDF as one section if no headings found
                full_content = "\n".join(pages_text)
                if full_content.strip():
                    sections.append({'title': 'Full Document Content', 'content': full_content.strip(), 'page': 1})
                return sections

            # Construct sections based on headings
            for idx, h_info in enumerate(headings):
                current_page_idx = h_info['page_index']
                start_char_on_page = h_info['start_char_index_on_page']
                content_buffer = ''

                if idx + 1 < len(headings):
                    next_h_info = headings[idx+1]
                    next_page_idx = next_h_info['page_index']
                    end_char_on_page_of_next = next_h_info['start_char_index_on_page']

                    if current_page_idx == next_page_idx:
                        # Section is within the same page
                        content_buffer += pages_text[current_page_idx][start_char_on_page:end_char_on_page_of_next]
                    else:
                        # Section spans multiple pages
                        content_buffer += pages_text[current_page_idx][start_char_on_page:] + '\n' # Rest of current page
                        for p_idx in range(current_page_idx + 1, next_page_idx): # Full intermediate pages
                            content_buffer += pages_text[p_idx] + '\n'
                        content_buffer += pages_text[next_page_idx][:end_char_on_page_of_next] # Start of next heading's page
                else: # Last heading, take content till end of document
                    content_buffer += pages_text[current_page_idx][start_char_on_page:] + '\n'
                    for p_idx in range(current_page_idx + 1, len(pages_text)):
                        content_buffer += pages_text[p_idx] + '\n'
                
                if content_buffer.strip():
                    sections.append({'title': h_info['title'], 'content': content_buffer.strip(), 'page': h_info['page']})
            
            # Filter out very small sections
            sections = [s for s in sections if len(s['content']) > max(50, len(s['title']) + 20)] 
            if not sections and "".join(pages_text).strip(): # If filtering removed everything but there's content
                 sections.append({'title': 'Full Document (Fallback)', 'content': "".join(pages_text).strip(), 'page': 1})
            return sections

        except Exception as e_fitz:
            print(f"Error during PDF processing with fitz: {e_fitz}. Attempting PyPDF2 fallback.")
            # Fall through to PyPDF2
    
    # PyPDF2 Fallback (if fitz failed or not available)
    try:
        from PyPDF2 import PdfReader
        if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0) # Ensure stream is at the beginning

        # PyPDF2 needs a file path or a file-like object.
        # If pdf_file_obj is a SpooledTemporaryFile from Gradio/FastAPI, it should work.
        reader = PdfReader(pdf_file_obj)
        
        text = ""
        for page_obj in reader.pages:
            extracted = page_obj.extract_text()
            if extracted:
                text += extracted + "\n"
        
        if not text.strip():
            return [{'title': 'PDF Error (PyPDF2)', 'content': 'No text extracted by PyPDF2.', 'page': None}]

        # Simple chunking for PyPDF2 as it loses formatting
        # This is a very basic way to split, might not be ideal for "sections"
        chunks = re.split(r'(?<=[.?!])\s+', text) # Split by sentences
        sections = []
        sentences_per_section_approx = 15 # Arbitrary number
        
        for i in range(0, len(chunks), sentences_per_section_approx):
            section_content = " ".join(chunks[i:i+sentences_per_section_approx]).strip()
            if section_content:
                sections.append({
                    'title': f'Content Block {i//sentences_per_section_approx + 1} (PyPDF2)', 
                    'content': section_content, 
                    'page': None # Page numbers are hard with PyPDF2's basic text extraction
                })
        
        if not sections and text.strip(): # If no sections created but text exists
            sections.append({'title': 'Full Document (PyPDF2)', 'content': text.strip(), 'page': None})
            
        return sections
    except ImportError:
        print("PyPDF2 not found. PDF processing capabilities are very limited.")
        return [{'title': 'PDF Library Error', 'content': 'Neither PyMuPDF (fitz) nor PyPDF2 are available.', 'page': None}]
    except Exception as e_pypdf2:
        print(f"Error processing PDF with PyPDF2: {e_pypdf2}")
        return [{'title': 'PDF Error (PyPDF2)', 'content': f'An error occurred: {e_pypdf2}', 'page': None}]


def download_docx(content, filename):
    buf = io.BytesIO()
    doc = Document()
    for line in content.split("\n"):
        p = doc.add_paragraph()
        parts = re.split(r'(\*\*.*?\*\*)', line) # Split by bold markdown
        for part in parts:
            if part.startswith('**') and part.endswith('**'):
                p.add_run(part[2:-2]).bold = True
            else:
                p.add_run(part)
    doc.save(buf)
    buf.seek(0)
    return buf, filename

def count_classes(start_date_obj, end_date_obj, weekday_indices):
    count = 0
    current_date = start_date_obj
    while current_date <= end_date_obj:
        if current_date.weekday() in weekday_indices:
            count += 1
        current_date += timedelta(days=1)
    return count

def generate_access_token(student_id, course_id, lesson_id, lesson_date_obj):
    if isinstance(lesson_date_obj, str):
        lesson_date_obj = datetime.strptime(lesson_date_obj, '%Y-%m-%d').date()
    
    iat_datetime_utc = datetime.combine(lesson_date_obj, datetime.min.time(), tzinfo=dt_timezone.utc).replace(hour=6)
    exp_datetime_utc = iat_datetime_utc + timedelta(hours=LINK_VALIDITY_HOURS)
    
    payload = {
        "sub": str(student_id), # Ensure student_id is string for JWT standard
        "course_id": str(course_id),
        "lesson_id": int(lesson_id),
        "iat": iat_datetime_utc,
        "exp": exp_datetime_utc,
        "aud": APP_DOMAIN
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)

def generate_5_digit_code():
    return str(random.randint(10000, 99999))

def send_email_notification(to_email, subject, html_content, from_name="AI Tutor System", attachment_file_obj=None, attachment_filename_override=None):
    if not SMTP_USER or not SMTP_PASS:
        print(f"CRITICAL SMTP ERROR: SMTP_USER or SMTP_PASS not configured. Cannot send email to {to_email}.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    # Ensure SMTP_USER is a valid email address for the From header
    msg["From"] = f"{from_name} <{SMTP_USER}>" if "@" in SMTP_USER else f"{from_name} <default_sender@example.com>" # Fallback if SMTP_USER is not email
    msg["To"] = to_email
    
    # Add Reply-To if from_name looks like an email (e.g., for support messages)
    if "@" in from_name and from_name.lower() != SMTP_USER.lower(): # Avoid self-reply-to
        msg.add_header('Reply-To', from_name)

    msg.add_alternative(html_content, subtype='html')

    if attachment_file_obj:
        filename_to_use = attachment_filename_override or \
                          (hasattr(attachment_file_obj, "name") and attachment_file_obj.name) or \
                          "attachment.dat"
        filename_to_use = os.path.basename(filename_to_use) # Sanitize

        try:
            attachment_file_obj.seek(0) # Ensure at the start of the stream
            file_data = attachment_file_obj.read()
            attachment_file_obj.seek(0) # Reset for potential reuse

            ctype, encoding = mimetypes.guess_type(filename_to_use)
            if ctype is None or encoding is not None:
                ctype = 'application/octet-stream' # Default MIME type
            maintype, subtype_val = ctype.split('/', 1)
            
            msg.add_attachment(file_data, maintype=maintype, subtype=subtype_val, filename=filename_to_use)
            print(f"Attachment '{filename_to_use}' prepared for email to {to_email}.")
        except Exception as e_attach:
            print(f"Error processing attachment '{filename_to_use}' for email to {to_email}: {e_attach}")
            # Optionally, decide if email should still be sent without attachment

    try:
        print(f"Attempting to send email titled '{subject}' to {to_email} via {SMTP_SERVER}:{SMTP_PORT} as {SMTP_USER}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s: # Increased timeout
            s.set_debuglevel(0) # 0 for production, 1 for detailed SMTP logs
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print(f"Email successfully sent to {to_email}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"SMTP Authentication Error for user {SMTP_USER}: {e}\nCheck SMTP_USER and SMTP_PASS.\n{traceback.format_exc()}")
        return False
    except smtplib.SMTPConnectError as e:
        print(f"SMTP Connect Error to {SMTP_SERVER}:{SMTP_PORT}: {e}\nCheck SMTP_SERVER and SMTP_PORT, and network connectivity.\n{traceback.format_exc()}")
        return False
    except smtplib.SMTPServerDisconnected as e:
        print(f"SMTP Server Disconnected: {e}\nThis might be a temporary server issue.\n{traceback.format_exc()}")
        return False
    except smtplib.SMTPRecipientsRefused as e:
        print(f"SMTP Recipient Refused for {to_email}: {e}\nThis usually means the recipient email address is invalid or rejected by their server.\n{traceback.format_exc()}")
        # You might want to log which specific recipient(s) were refused if sending to multiple via BCC, etc.
        return False
    except smtplib.SMTPException as e:
        print(f"General SMTP Exception for email to {to_email}: {e}\n{traceback.format_exc()}")
        return False
    except Exception as e:
        print(f"Unexpected error sending email to {to_email}: {e}\n{traceback.format_exc()}")
        return False

# --- Syllabus & Lesson Plan Generation (Instructor Panel) ---
def generate_syllabus(cfg):
    sd_str, ed_str = cfg.get('start_date'), cfg.get('end_date')
    if not sd_str or not ed_str: return "Error: Start or end date missing in config."
    try:
        sd = datetime.strptime(sd_str, '%Y-%m-%d').date()
        ed = datetime.strptime(ed_str, '%Y-%m-%d').date()
    except ValueError:
        return "Error: Invalid date format in config."

    month_range_str = f"{sd.strftime('%B %Y')} – {ed.strftime('%B %Y')}"
    if sd.year == ed.year:
        month_range_str = f"{sd.strftime('%B')} – {ed.strftime('%B %Y')}" if sd.month != ed.month else f"{sd.strftime('%B %Y')}"


    total_classes_val = count_classes(sd, ed, [days_map[d] for d in cfg.get('class_days', [])])
    
    header = [
        f"**Course:** {cfg.get('course_name', 'N/A')}",
        f"**Instructor:** {cfg.get('instructor', {}).get('name', 'N/A')}",
        f"**Email:** {cfg.get('instructor', {}).get('email', 'N/A')}",
        f"**Duration:** {month_range_str} ({total_classes_val} classes)",
        '-'*60
    ]
    objectives_list = [f" • {obj}" for obj in cfg.get('learning_objectives', [])]
    
    body_content = [
        "**COURSE DESCRIPTION:**", 
        cfg.get('course_description', 'Not specified.'), "",
        "**LEARNING OBJECTIVES:**"
    ] + objectives_list + [
        "", "**GRADING POLICY:**",
        " • Participation in AI Tutor sessions.",
        " • Completion of per-lesson quizzes (retake if score < 60%).",
        " • Final understanding assessed based on overall engagement and quiz performance.", "",
        "**CLASS SCHEDULE:**",
        f" • Classes typically run on: {', '.join(cfg.get('class_days', ['N/A']))}",
        f" • Check daily email reminders for exact lesson access links.", "",
        "**SUPPORT & OFFICE HOURS:**",
        " • For technical issues with the AI Tutor, use the 'Contact Support' tab in the Instructor Panel.",
        " • For course content questions, please reach out to your instructor directly."
        # Placeholder office hours, customize as needed
        # " • Office Hours: Tue 3–5PM; Thu 10–11AM (Zoom link to be provided by instructor)"
    ]
    return "\n".join(header + [""] + body_content)


def generate_plan_by_week_structured_and_formatted(cfg):
    # ... (This function is complex, ensure the version from your previous iterations is used if it was working well for character segmentation and page mapping)
    # The version provided in the prompt had detailed logic for this. I will use its structure.
    sd_str, ed_str = cfg.get('start_date'), cfg.get('end_date')
    if not sd_str or not ed_str: return "Error: Start or end date missing.", []
    try:
        sd, ed = datetime.strptime(sd_str, '%Y-%m-%d').date(), datetime.strptime(ed_str, '%Y-%m-%d').date()
    except ValueError: return "Error: Invalid date format.", []

    selected_weekdays = {days_map[d] for d in cfg.get('class_days', [])}
    if not selected_weekdays: return "Error: No class days selected.", []

    class_dates_list = [current_date for i in range((ed - sd).days + 1) if (current_date := sd + timedelta(days=i)).weekday() in selected_weekdays]
    
    print(f"DEBUG (generate_plan): Found {len(class_dates_list)} class dates between {sd} and {ed} for days: {cfg.get('class_days')}.")

    if not class_dates_list: return "No class dates fall within the specified range and selected weekdays.", []

    full_text_content = cfg.get("full_text_content", "")
    char_offset_map = cfg.get("char_offset_page_map", [])

    if not full_text_content.strip():
        print("Warning (generate_plan): Full PDF text content is empty. Generating placeholder lesson topics.")
        placeholder_lessons, placeholder_lines, lessons_by_course_week_dict = [], [], {}
        
        course_week_counter_ph = 0
        current_week_monday_for_grouping_ph = None
        for idx, dt_obj in enumerate(class_dates_list):
            monday_of_this_week_ph = dt_obj - timedelta(days=dt_obj.weekday())
            if current_week_monday_for_grouping_ph is None or monday_of_this_week_ph > current_week_monday_for_grouping_ph:
                course_week_counter_ph += 1
                current_week_monday_for_grouping_ph = monday_of_this_week_ph
            
            year_of_this_course_week_ph = monday_of_this_week_ph.year
            course_week_key_ph = f"{year_of_this_course_week_ph}-CW{course_week_counter_ph:02d}"
            
            lesson_entry = {"lesson_number": idx + 1, "date": dt_obj.strftime('%Y-%m-%d'), 
                            "topic_summary": "Topic TBD (No PDF text provided)", 
                            "original_section_title": "N/A", "page_reference": None}
            placeholder_lessons.append(lesson_entry)
            lessons_by_course_week_dict.setdefault(course_week_key_ph, []).append(lesson_entry)
        
        for course_wk_key_ph_sorted in sorted(lessons_by_course_week_dict.keys()):
            year_disp_ph, course_week_num_disp_str_ph = course_wk_key_ph_sorted.split("-CW")
            first_date_in_group_ph = lessons_by_course_week_dict[course_wk_key_ph_sorted][0]['date']
            year_of_first_date_ph = datetime.strptime(first_date_in_group_ph, '%Y-%m-%d').year
            placeholder_lines.append(f"**Course Week {int(course_week_num_disp_str_ph)} (Year {year_of_first_date_ph})**\n")
            for lsn_item in lessons_by_course_week_dict[course_wk_key_ph_sorted]:
                placeholder_lines.append(f"**Lesson {lsn_item['lesson_number']} ({datetime.strptime(lsn_item['date'], '%Y-%m-%d').strftime('%B %d, %Y')})**: {lsn_item['topic_summary']}")
            placeholder_lines.append('')
        return "\n".join(placeholder_lines), placeholder_lessons

    total_chars_in_text = len(full_text_content)
    num_lessons_to_plan = len(class_dates_list)
    chars_per_lesson_segment = total_chars_in_text // num_lessons_to_plan if num_lessons_to_plan > 0 else total_chars_in_text
    
    min_chars_for_summary = 150  # Minimum characters to attempt a summary
    lesson_topic_summaries = []
    current_char_pointer = 0
    segment_start_chars = []

    print(f"DEBUG (generate_plan): Total chars: {total_chars_in_text}, Chars/lesson: {chars_per_lesson_segment} for {num_lessons_to_plan} lessons.")

    client = openai.OpenAI() # Initialize client for OpenAI calls
    for i in range(num_lessons_to_plan):
        segment_start_chars.append(current_char_pointer)
        start_index = current_char_pointer
        end_index = current_char_pointer + chars_per_lesson_segment if i < num_lessons_to_plan - 1 else total_chars_in_text
        
        text_segment_for_summary = full_text_content[start_index:end_index].strip()
        current_char_pointer = end_index

        if len(text_segment_for_summary) < min_chars_for_summary:
            lesson_topic_summaries.append("Review of previous topics or brief discussion.")
            print(f"DEBUG (generate_plan): Segment {i+1} too short (len {len(text_segment_for_summary)}), using default summary.")
        else:
            try:
                # print(f"DEBUG (generate_plan): Summarizing segment {i+1} (len {len(text_segment_for_summary)}): '{text_segment_for_summary[:70].replace(chr(10),' ')}...'")
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo", # Cheaper model for summaries
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant. Identify the core concept from the provided text. Respond ONLY with a short, concise phrase (maximum 10-12 words, ideally a gerund phrase like 'Using verbs effectively') suitable as a lesson topic title. Do NOT use full sentences or any introductory/concluding remarks."},
                        {"role": "user", "content": text_segment_for_summary}
                    ],
                    temperature=0.4,
                    max_tokens=30 
                )
                summary = response.choices[0].message.content.strip().replace('"', '').replace('.', '').capitalize()
                if not summary: summary = f"Content Analysis Segment {i+1}" # Fallback if AI returns empty
                lesson_topic_summaries.append(summary)
            except Exception as e:
                print(f"Error summarizing segment {i+1} with OpenAI: {e}")
                lesson_topic_summaries.append(f"Topic for segment {i+1} (AI summary error)")
    
    lessons_by_course_week_dict = {}
    structured_lessons_list = []
    
    course_week_counter = 0
    current_week_monday_for_grouping = None

    for idx, dt_obj in enumerate(class_dates_list):
        monday_of_this_week = dt_obj - timedelta(days=dt_obj.weekday())
        if current_week_monday_for_grouping is None or monday_of_this_week > current_week_monday_for_grouping:
            course_week_counter += 1
            current_week_monday_for_grouping = monday_of_this_week
        
        year_of_this_course_week = monday_of_this_week.year
        course_week_key = f"{year_of_this_course_week}-CW{course_week_counter:02d}"

        summary_for_this_lesson = lesson_topic_summaries[idx] if idx < len(lesson_topic_summaries) else "Topic TBD"
        
        estimated_page_ref = None
        if char_offset_map: # Ensure map exists
            seg_start_char_offset = segment_start_chars[idx]
            # Find the page number corresponding to the start of the segment
            for offset, page_num in reversed(char_offset_map): # Iterate backwards
                if seg_start_char_offset >= offset:
                    estimated_page_ref = page_num
                    break
            if estimated_page_ref is None and char_offset_map: # If still None, use first page
                estimated_page_ref = char_offset_map[0][1] 
        
        lesson_entry = {
            "lesson_number": idx + 1, 
            "date": dt_obj.strftime('%Y-%m-%d'),
            "topic_summary": summary_for_this_lesson, 
            "original_section_title": f"Text Segment {idx+1}", # Placeholder, could try to map to PDF sections if available
            "page_reference": estimated_page_ref 
        }
        structured_lessons_list.append(lesson_entry)
        lessons_by_course_week_dict.setdefault(course_week_key, []).append(lesson_entry)

    formatted_plan_lines = []
    for course_wk_key_sorted in sorted(lessons_by_course_week_dict.keys()):
        # year_disp, course_week_num_disp_str = course_wk_key_sorted.split("-CW")
        # course_week_num_disp = int(course_week_num_disp_str)
        
        # Use the year of the first actual class date in that week group for display
        first_lesson_in_group = lessons_by_course_week_dict[course_wk_key_sorted][0]
        first_date_obj_in_group = datetime.strptime(first_lesson_in_group['date'], '%Y-%m-%d')
        # Extract course week number from the key for display
        course_week_num_from_key = int(course_wk_key_sorted.split("-CW")[1])

        formatted_plan_lines.append(f"**Course Week {course_week_num_from_key} (Year {first_date_obj_in_group.year})**\n")
        
        for lesson_item in lessons_by_course_week_dict[course_wk_key_sorted]:
            date_str_formatted = datetime.strptime(lesson_item['date'], '%Y-%m-%d').strftime('%B %d, %Y')
            page_ref_str = f" (Approx. Ref. p. {lesson_item['page_reference']})" if lesson_item['page_reference'] else ''
            formatted_plan_lines.append(f"**Lesson {lesson_item['lesson_number']} ({date_str_formatted})**{page_ref_str}: {lesson_item['topic_summary']}")
        formatted_plan_lines.append('')
        
    return "\n".join(formatted_plan_lines), structured_lessons_list

# --- APScheduler Jobs ---
def send_daily_class_reminders():
    now_utc = datetime.now(dt_timezone.utc)
    today_utc_date = now_utc.date()
    print(f"SCHEDULER: Running daily class reminder job at {now_utc.isoformat()}")
    print(f"SCHEDULER: Today's date (UTC) for matching: {today_utc_date}")

    course_configs_found = 0
    reminders_sent_total = 0

    for config_file in CONFIG_DIR.glob("*_config.json"):
        course_configs_found += 1
        course_id_from_filename = config_file.stem.replace("_config", "")
        print(f"SCHEDULER: Processing course config: {config_file.name} (Course ID: {course_id_from_filename})")
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            course_name = cfg.get("course_name", course_id_from_filename)
            
            lessons = cfg.get("lessons")
            students = cfg.get("students")

            if not lessons:
                print(f"SCHEDULER: No lessons found in config for '{course_name}'. Skipping.")
                continue
            if not students:
                print(f"SCHEDULER: No students found in config for '{course_name}'. Skipping.")
                continue

            print(f"SCHEDULER: Found {len(lessons)} lessons and {len(students)} students for '{course_name}'.")

            for lesson in lessons:
                lesson_number = lesson.get("lesson_number", "N/A")
                lesson_date_str = lesson.get("date")
                lesson_topic = lesson.get('topic_summary', 'Lesson Topic')
                
                if not lesson_date_str:
                    print(f"SCHEDULER: Lesson {lesson_number} in '{course_name}' is missing a date. Skipping.")
                    continue
                
                try:
                    lesson_date_obj = datetime.strptime(lesson_date_str, '%Y-%m-%d').date()
                except ValueError:
                    print(f"SCHEDULER: Invalid date format '{lesson_date_str}' for lesson {lesson_number} in '{course_name}'. Skipping.")
                    continue

                # print(f"SCHEDULER: Checking Lesson {lesson_number} ({lesson_date_obj}) for '{course_name}' against {today_utc_date}.")
                if lesson_date_obj == today_utc_date:
                    print(f"SCHEDULER: MATCH! Class found for '{course_name}' today: Lesson {lesson_number} - Topic: {lesson_topic}")
                    
                    class_code = generate_5_digit_code()
                    reminders_sent_for_this_class = 0
                    for student in students:
                        student_id = student.get("id", f"unknown_student_{uuid.uuid4()}")
                        student_email = student.get("email")
                        student_name = student.get("name", "Student")

                        if not student_email:
                            print(f"SCHEDULER: Student '{student_name}' ({student_id}) in '{course_name}' is missing an email. Skipping.")
                            continue
                        
                        try:
                            token = generate_access_token(student_id, course_id_from_filename, lesson_number, lesson_date_obj)
                            access_link = f"{APP_DOMAIN}/class?token={token}"
                            
                            email_subject = f"Today's AI Tutor Lesson for {course_name}: {lesson_topic}"
                            email_html_body = f"""
                            <html><head><style>body {{font-family: Arial, sans-serif; margin: 0; padding: 0; background-color: #f4f4f4;}} .container {{max-width: 600px; margin: 20px auto; background-color: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1);}} h2 {{color: #333333;}} p {{color: #555555; line-height: 1.6;}} .button {{display: inline-block; background-color: #007bff; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold;}} .code {{font-size: 1.2em; font-weight: bold; color: #28a745; background-color: #e9ecef; padding: 3px 8px; border-radius: 4px;}} .footer {{font-size: 0.9em; color: #777777; margin-top: 20px; text-align: center;}}</style></head>
                            <body><div class="container">
                                <h2>Your AI Tutor Lesson is Ready!</h2>
                                <p>Hi {student_name},</p>
                                <p>Your AI Tutor session for <strong>{course_name}</strong> on the topic "<strong>{lesson_topic}</strong>" is scheduled for today.</p>
                                <p><a href="{access_link}" class="button">Access Your Lesson</a></p>
                                <p>If the button doesn't work, copy and paste this link into your browser:<br><a href="{access_link}">{access_link}</a></p>
                                <p>Your reference code for today's session is: <span class="code">{class_code}</span> (You usually won't need this code if you use the link).</p>
                                <p>This link is valid from <strong>6:00 AM to 12:00 PM UTC</strong> on <strong>{lesson_date_obj.strftime('%B %d, %Y')}</strong>.</p>
                                <p>Happy learning!</p>
                                <div class="footer">The AI Tutor System</div>
                            </div></body></html>"""
                            
                            print(f"SCHEDULER: Attempting to send reminder to {student_name} <{student_email}> for lesson {lesson_number} of '{course_name}'.")
                            # Pass student name to from_name for context if needed, but keep system identity for main sender
                            if send_email_notification(student_email, email_subject, email_html_body, from_name=f"{STUDENT_BOT_NAME} ({course_name})"):
                                reminders_sent_total += 1
                                reminders_sent_for_this_class += 1
                            else:
                                print(f"SCHEDULER: Failed to send reminder to {student_name} <{student_email}> for lesson {lesson_number}.")
                        except Exception as e_token_email:
                            print(f"SCHEDULER: Error generating token or preparing email for student {student_id} in '{course_name}': {e_token_email}\n{traceback.format_exc()}")
                    print(f"SCHEDULER: Sent {reminders_sent_for_this_class} reminders for Lesson {lesson_number} of '{course_name}'.")
        except FileNotFoundError:
            print(f"SCHEDULER: Config file {config_file.name} not found during glob iteration. This shouldn't happen if CONFIG_DIR is correct. Skipping.")
        except json.JSONDecodeError:
            print(f"SCHEDULER: Error decoding JSON from {config_file.name}. The file might be corrupted. Skipping.")
        except Exception as e:
            print(f"SCHEDULER: General error processing config {config_file.name}: {e}\n{traceback.format_exc()}")
    
    if course_configs_found == 0:
        print(f"SCHEDULER: No course configuration files found in '{CONFIG_DIR}'. No reminders processed.")
    print(f"SCHEDULER: Daily class reminder job finished. Total reminders sent: {reminders_sent_total}")


def log_student_progress(student_id, course_id, lesson_id, quiz_score_str, session_duration_secs, engagement_notes="N/A"):
    if not PROGRESS_LOG_FILE.exists():
        with open(PROGRESS_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_utc", "student_id", "course_id", "lesson_id", "quiz_score", "session_duration_seconds", "engagement_notes"])
    
    with open(PROGRESS_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(dt_timezone.utc).isoformat(), student_id, course_id, lesson_id, quiz_score_str, round(session_duration_secs, 2), engagement_notes])
    print(f"Progress logged: Student {student_id}, Course {course_id}, Lesson {lesson_id}, Score {quiz_score_str}, Duration {session_duration_secs:.0f}s.")


def check_student_progress_and_notify_professor():
    print(f"SCHEDULER: Running student progress check at {datetime.now(dt_timezone.utc).isoformat()}")
    if not PROGRESS_LOG_FILE.exists():
        print("SCHEDULER (Progress Check): Progress log file does not exist. Skipping check.")
        return

    one_day_ago_utc = datetime.now(dt_timezone.utc) - timedelta(days=1)
    alerts_by_course_instructor = {} # {instructor_email: {course_name: [alert_messages]}}

    try:
        with open(PROGRESS_LOG_FILE, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader):
                try:
                    log_timestamp_utc = datetime.fromisoformat(row["timestamp_utc"])
                    if log_timestamp_utc < one_day_ago_utc:
                        continue # Only process recent logs

                    quiz_score_str = row.get("quiz_score", "0/0")
                    if "/" in quiz_score_str:
                        parts = quiz_score_str.split('/')
                        if len(parts) == 2:
                            correct, total_qs = map(int, parts)
                            if total_qs > 0 and (correct / total_qs) < 0.60: # Threshold: < 60%
                                student_id = row["student_id"]
                                course_id = row["course_id"]
                                lesson_id = row["lesson_id"]
                                duration = row.get('session_duration_seconds','N/A')
                                notes = row.get('engagement_notes','N/A')

                                # Get instructor email from course config
                                config_path = CONFIG_DIR / f"{course_id.replace(' ','_').lower()}_config.json"
                                if not config_path.exists():
                                    print(f"SCHEDULER (Progress Check): Config for course {course_id} not found. Cannot notify instructor.")
                                    continue
                                
                                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                                instructor_info = cfg.get("instructor", {})
                                instructor_email = instructor_info.get("email")
                                instructor_name = instructor_info.get("name", "Instructor")
                                course_name_from_cfg = cfg.get("course_name", course_id)

                                if instructor_email:
                                    alert_msg = (f"Student <strong>{student_id}</strong> scored {quiz_score_str} "
                                                 f"on lesson {lesson_id} (Session: {duration}s, Logged: {log_timestamp_utc.strftime('%Y-%m-%d %H:%M')} UTC). "
                                                 f"Notes: <em>{notes}</em>")
                                    
                                    instructor_alerts = alerts_by_course_instructor.setdefault(instructor_email, {})
                                    course_alerts = instructor_alerts.setdefault(course_name_from_cfg, {"name": instructor_name, "alerts": []})
                                    course_alerts["alerts"].append(alert_msg)
                        else:
                             print(f"SCHEDULER (Progress Check): Malformed quiz_score '{quiz_score_str}' in log row {row_num+1}. Skipping.")
                except ValueError as ve:
                    print(f"SCHEDULER (Progress Check): Skipping malformed row {row_num+1} in progress log: {ve} - Row: {row}")
                except Exception as e_row:
                     print(f"SCHEDULER (Progress Check): Error processing row {row_num+1}: {e_row} - Row: {row}")


    except Exception as e_read_log:
        print(f"SCHEDULER (Progress Check): Error reading progress log '{PROGRESS_LOG_FILE}': {e_read_log}")
        return

    # Send collated alerts
    for instructor_email, courses_data in alerts_by_course_instructor.items():
        full_alert_html_body = f"<html><body style='font-family: sans-serif;'><p>Dear {courses_data.get(next(iter(courses_data)), {}).get('name', 'Instructor')},</p>"
        full_alert_html_body += "<p>One or more students may require attention based on recent AI Tutor sessions:</p>"
        
        any_alerts_for_this_instructor = False
        for course_name, data in courses_data.items():
            if data["alerts"]:
                any_alerts_for_this_instructor = True
                full_alert_html_body += f"<h3>Course: {course_name}</h3><ul>"
                for alert in data["alerts"]:
                    full_alert_html_body += f"<li>{alert}</li>"
                full_alert_html_body += "</ul>"
        
        if any_alerts_for_this_instructor:
            full_alert_html_body += (f"<p>Please consider reviewing their progress and engaging with them directly.</p>"
                                     f"<p>Best regards,<br>AI Tutor Monitoring System</p></body></html>")
            subject = f"AI Tutor: Student Progress Summary"
            send_email_notification(instructor_email, subject, full_alert_html_body, "AI Tutor Monitoring")
            print(f"SCHEDULER (Progress Check): Sent progress alert summary to {instructor_email}.")
        else:
            print(f"SCHEDULER (Progress Check): No new low scores to report for instructor {instructor_email} after filtering.")
    print(f"SCHEDULER (Progress Check): Finished.")


# --- Gradio Callbacks (Instructor Panel - largely as before, minor cleanups) ---
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
    try: return json.loads(path.read_text(encoding="utf-8")).get("lesson_plan_formatted", "Plan not generated or not found in config.")
    except Exception as e: return f"Error loading plan: {e}"

def enable_edit_syllabus_and_reload(current_course_name, current_output_content):
    if not current_output_content.strip().startswith("**Course:**"): 
        syllabus_text = _get_syllabus_text_from_config(current_course_name)
        return gr.update(value=syllabus_text, interactive=True)
    return gr.update(interactive=True)

def enable_edit_plan_and_reload(current_course_name_for_plan, current_plan_output_content):
    # Check if content is not a plan (e.g. error message or success message)
    is_error_or_status = current_plan_output_content.strip().startswith("⚠️") or \
                         current_plan_output_content.strip().startswith("✅") or \
                         not current_plan_output_content.strip().startswith("**Course Week")
    
    if is_error_or_status:
        plan_text = _get_plan_text_from_config(current_course_name_for_plan)
        return gr.update(value=plan_text, interactive=True) # Reload original if current is not plan
    return gr.update(interactive=True) # Otherwise, just make it editable


def save_setup(course_name, instr_name, instr_email, devices_cb_group, pdf_file_obj,
               start_year, start_month, start_day, end_year, end_month, end_day,
               class_days_checklist, students_csv_text):
    
    # Define the full tuple structure for return, makes it easier to manage
    # Order must match the `outputs` in `btn_save.click()`
    # output_box, btn_save, dummy_btn_1, btn_generate_plan,
    # btn_edit_syl, btn_email_syl, btn_edit_plan,
    # btn_email_plan, syllabus_actions_row, plan_buttons_row,
    # output_plan_box, lesson_plan_setup_message, course_load_for_plan
    
    num_expected_outputs = 13 # Keep track of how many outputs are expected
    def error_return_tuple(error_message_str):
        # Default visibility for action rows/buttons is False
        return (
            gr.update(value=error_message_str, visible=True, interactive=False), # output_box
            gr.update(visible=True), # btn_save (remains visible)
            gr.Button.update(visible=False), # dummy_btn_1
            gr.Button.update(visible=True),  # btn_generate_plan (remains visible or becomes visible if hidden)
            gr.Button.update(visible=False), # btn_edit_syl
            gr.Button.update(visible=False), # btn_email_syl
            gr.Button.update(visible=False), # btn_edit_plan
            gr.Button.update(visible=False), # btn_email_plan
            gr.Row.update(visible=False),    # syllabus_actions_row
            gr.Row.update(visible=False),    # plan_buttons_row
            gr.Textbox.update(value="", visible=False), # output_plan_box
            gr.Markdown.update(visible=True),# lesson_plan_setup_message (stays visible or show error)
            gr.Textbox.update(value=course_name if course_name else "", visible=False) # course_load_for_plan
        )

    try:
        required_fields = {
            "Course Name": course_name, "Instructor Name": instr_name, "Instructor Email": instr_email,
            "PDF Material": pdf_file_obj, "Start Year": start_year, "Start Month": start_month, "Start Day": start_day,
            "End Year": end_year, "End Month": end_month, "End Day": end_day,
            "Class Days": class_days_checklist
        }
        missing = [name for name, val in required_fields.items() if not val]
        if missing:
            return error_return_tuple(f"⚠️ Error: Required fields missing: {', '.join(missing)}.")

        try:
            start_dt_obj = datetime(int(start_year), int(start_month), int(start_day)).date()
            end_dt_obj = datetime(int(end_year), int(end_month), int(end_day)).date()
            if end_dt_obj <= start_dt_obj:
                return error_return_tuple("⚠️ Error: Course end date must be after the start date.")
        except ValueError:
            return error_return_tuple("⚠️ Error: Invalid date selected for course schedule.")

        # PDF Processing for description and full text
        # Reset file pointer for multiple reads if it's a file-like object
        if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0)
        sections_for_desc = split_sections(pdf_file_obj) # Assumes split_sections can handle the file obj
        
        if not sections_for_desc or (len(sections_for_desc) == 1 and "Error" in sections_for_desc[0].get('title', '')):
             return error_return_tuple("⚠️ Error: Could not extract structural sections from PDF for analysis. Try a different PDF or check its format.")

        full_pdf_text_content = ""
        char_offset_to_page_map_list = []
        current_char_offset_val = 0

        if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0) # Reset again for full text extraction

        # Prioritize fitz for full text and page mapping
        fitz_used_for_full_text = False
        if fitz_available:
            doc_for_full_text_extraction = None
            try:
                if hasattr(pdf_file_obj, "name") and pdf_file_obj.name: # If it's a file with a path
                    doc_for_full_text_extraction = fitz.open(pdf_file_obj.name)
                elif hasattr(pdf_file_obj, "read"): # If it's a file-like object (e.g. SpooledTemporaryFile)
                    pdf_bytes_content = pdf_file_obj.read()
                    pdf_file_obj.seek(0) # Reset pointer after read
                    doc_for_full_text_extraction = fitz.open(stream=pdf_bytes_content, filetype="pdf")
                
                if doc_for_full_text_extraction:
                    for page_num_fitz, page_obj_fitz in enumerate(doc_for_full_text_extraction):
                        page_text_content = page_obj_fitz.get_text("text", sort=True)
                        if page_text_content: # Only add if text exists
                            char_offset_to_page_map_list.append((current_char_offset_val, page_num_fitz + 1))
                            full_pdf_text_content += page_text_content + "\n" # Add newline as page separator
                            current_char_offset_val += len(page_text_content) + 1 # Account for newline
                    doc_for_full_text_extraction.close()
                    fitz_used_for_full_text = True
                else: # Should not happen if open was successful
                    print("Warning (save_setup): fitz.open seemed successful but doc object is None.")
            except Exception as e_fitz_full_text:
                print(f"Error extracting full text with fitz: {e_fitz_full_text}. Will fallback if possible.")
        
        if not fitz_used_for_full_text or not full_pdf_text_content.strip():
            print("Warning (save_setup): Fitz not used or failed for full text. Using concatenated section content. Page map may be inaccurate or empty.")
            if hasattr(pdf_file_obj, "seek"): pdf_file_obj.seek(0)
            # Use sections_for_desc (already extracted) if fitz failed for full text
            full_pdf_text_content = "\n\n".join(s['content'] for s in sections_for_desc) # Fallback full text
            char_offset_to_page_map_list = [] # Page map likely lost or inaccurate with this fallback
            # Could try to build a crude map if sections_for_desc has page numbers from fitz initial pass
            if sections_for_desc and sections_for_desc[0].get('page') is not None:
                temp_offset = 0
                for s_item in sections_for_desc:
                    char_offset_to_page_map_list.append((temp_offset, s_item['page']))
                    temp_offset += len(s_item['content']) + 2 # +2 for \n\n

        if not full_pdf_text_content.strip():
            return error_return_tuple("⚠️ Error: Extracted PDF text content is empty. Cannot proceed.")

        # AI-generated content
        # Create a concise summary of the PDF content for AI prompts
        content_sample_for_ai = "\n\n".join(
            f"Section Title (Page {s.get('page', 'N/A')}): {s['title']}\nContent Snippet: {s['content'][:500]}..." 
            for s in sections_for_desc[:5] # Use first few sections for brevity
        )
        if len(content_sample_for_ai) > 8000: # Truncate if too long for prompt
            content_sample_for_ai = content_sample_for_ai[:8000] + "..."
        
        client = openai.OpenAI()
        try:
            desc_response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Based on the following course material snippets, generate a concise and engaging course description (2-3 sentences max)."},
                    {"role": "user", "content": content_sample_for_ai}
                ]
            )
            course_desc_text = desc_response.choices[0].message.content.strip()

            obj_response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Based on the following course material snippets, generate 5-7 clear, actionable learning objectives. Each objective should start with an action verb (e.g., 'Understand...', 'Apply...', 'Analyze...'). List them one per line."},
                    {"role": "user", "content": content_sample_for_ai}
                ]
            )
            learning_objs_list = [line.strip(" -•*") for line in obj_response.choices[0].message.content.splitlines() if line.strip()]
        except openai.APIError as oai_err:
            print(f"OpenAI API Error during course setup: {oai_err}\n{traceback.format_exc()}")
            return error_return_tuple(f"⚠️ OpenAI API Error: {oai_err}. Check API key and service status.")


        # Parse students
        parsed_students_list = []
        if students_csv_text:
            for line_num, line_str in enumerate(students_csv_text.splitlines()):
                if line_str.strip():
                    parts = [p.strip() for p in line_str.split(',')]
                    if len(parts) >= 2:
                        s_name, s_email = parts[0], parts[1]
                        if s_name and "@" in s_email: # Basic validation
                            parsed_students_list.append({"id": str(uuid.uuid4()), "name": s_name, "email": s_email})
                        else:
                            print(f"Warning (save_setup): Skipping invalid student line {line_num+1}: '{line_str}'")
                    else:
                        print(f"Warning (save_setup): Skipping malformed student line {line_num+1}: '{line_str}'")
        
        # Create config dictionary
        config_data = {
            "course_name": course_name,
            "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days_checklist,
            "start_date": start_dt_obj.strftime('%Y-%m-%d'),
            "end_date": end_dt_obj.strftime('%Y-%m-%d'),
            "allowed_devices": devices_cb_group,
            "students": parsed_students_list,
            # "sections_for_description": sections_for_desc, # Maybe too much detail for config, full_text is key
            "full_text_content": full_pdf_text_content,
            "char_offset_page_map": char_offset_to_page_map_list,
            "course_description": course_desc_text,
            "learning_objectives": learning_objs_list,
            "lessons": [], # To be populated by lesson plan generation
            "lesson_plan_formatted": "" # To be populated
        }
        
        config_file_path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        config_file_path.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")
        
        syllabus_display_text = generate_syllabus(config_data)
        
        return (
            gr.update(value=syllabus_display_text, visible=True, interactive=False), # output_box
            gr.update(visible=False), # btn_save (hide after success)
            gr.Button.update(visible=False), # dummy_btn_1
            gr.Button.update(visible=True),  # btn_generate_plan
            gr.Button.update(visible=True),  # btn_edit_syl
            gr.Button.update(visible=True),  # btn_email_syl
            gr.Button.update(visible=False), # btn_edit_plan (until plan generated)
            gr.Button.update(visible=False), # btn_email_plan (until plan generated)
            gr.Row.update(visible=True),     # syllabus_actions_row
            gr.Row.update(visible=True),     # plan_buttons_row (activate generate plan button visibility)
            gr.Textbox.update(value="Lesson plan not yet generated.", visible=False), # output_plan_box (initially hidden or shows message)
            gr.Markdown.update(visible=False),# lesson_plan_setup_message (hide this now)
            gr.Textbox.update(value=course_name, visible=True) # course_load_for_plan (populate and show)
        )

    except Exception as e:
        print(f"Error in save_setup: {e}\n{traceback.format_exc()}")
        return error_return_tuple(f"⚠️ An unexpected error occurred: {e}")


def generate_plan_callback(course_name_for_plan):
    # Expected outputs:
    # output_plan_box, dummy_btn_2, dummy_btn_1,
    # btn_generate_plan, dummy_btn_3, dummy_btn_4,
    # btn_edit_plan, btn_email_plan
    num_expected_outputs_plan = 8
    def error_return_for_plan_gen(error_message_str):
        return (
            gr.update(value=error_message_str, visible=True, interactive=False), # output_plan_box
            gr.Button.update(visible=False), # dummy_btn_2
            gr.Button.update(visible=False), # dummy_btn_1
            gr.Button.update(visible=True),  # btn_generate_plan (remains active)
            gr.Button.update(visible=False), # dummy_btn_3
            gr.Button.update(visible=False), # dummy_btn_4
            gr.Button.update(visible=False), # btn_edit_plan
            gr.Button.update(visible=False)  # btn_email_plan
        )

    try:
        if not course_name_for_plan:
            return error_return_for_plan_gen("⚠️ Error: Course Name is required to generate a lesson plan.")
        
        config_file_path = CONFIG_DIR / f"{course_name_for_plan.replace(' ','_').lower()}_config.json"
        if not config_file_path.exists():
            return error_return_for_plan_gen(f"⚠️ Error: Configuration file for '{course_name_for_plan}' not found. Please complete Course Setup first.")
        
        cfg_data = json.loads(config_file_path.read_text(encoding="utf-8"))
        
        # Ensure necessary fields from setup are present
        if not cfg_data.get("full_text_content") or not cfg_data.get("start_date") or \
           not cfg_data.get("end_date") or not cfg_data.get("class_days"):
            return error_return_for_plan_gen("⚠️ Error: Course config is incomplete (missing text, schedule, or days). Please re-run Course Setup.")

        formatted_plan_str, structured_lessons_list = generate_plan_by_week_structured_and_formatted(cfg_data)
        
        if "Error:" in formatted_plan_str : # Check if generate_plan_by_week returned an error string
             return error_return_for_plan_gen(f"⚠️ Error generating plan: {formatted_plan_str}")

        cfg_data["lessons"] = structured_lessons_list 
        cfg_data["lesson_plan_formatted"] = formatted_plan_str
        config_file_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8")

        class_days_str = ", ".join(cfg_data.get("class_days", ["as per configured schedule"]))
        notification_message_for_plan = (
            f"\n\n---\n✅ **Lesson Plan Generated & Daily Email Reminders Activated!**\n"
            f"Students in this course will now receive email reminders with a unique link "
            f"to their AI Tutor lesson on each scheduled class day ({class_days_str}). "
            f"These links are typically active from 6:00 AM to 12:00 PM UTC on those days."
        )
        display_text_for_plan_output_box = formatted_plan_str + notification_message_for_plan
        
        return (
            gr.update(value=display_text_for_plan_output_box, visible=True, interactive=False), # output_plan_box
            gr.Button.update(visible=False), # dummy_btn_2
            gr.Button.update(visible=False), # dummy_btn_1
            gr.Button.update(visible=True, value="Re-generate Lesson Plan"),  # btn_generate_plan (text changes)
            gr.Button.update(visible=False), # dummy_btn_3
            gr.Button.update(visible=False), # dummy_btn_4
            gr.Button.update(visible=True),  # btn_edit_plan
            gr.Button.update(visible=True)   # btn_email_plan
        )
    except openai.APIError as oai_err:
        print(f"OpenAI API Error during plan generation: {oai_err}\n{traceback.format_exc()}")
        return error_return_for_plan_gen(f"⚠️ OpenAI API Error: {oai_err}. Check API key and service status.")
    except Exception as e:
        print(f"Error in generate_plan_callback: {e}\n{traceback.format_exc()}")
        return error_return_for_plan_gen(f"⚠️ An unexpected error occurred: {e}")


def email_document_callback(course_name_str, document_type_str, output_text_content_str, students_text_input_str):
    # This function handles both syllabus and lesson plan emailing
    if not SMTP_USER or not SMTP_PASS:
        return gr.update(value="⚠️ Error: SMTP server settings (user/pass) are not configured in the environment. Cannot send emails.")
    
    status_message_prefix = f"Emailing {document_type_str}"
    try:
        if not course_name_str or not output_text_content_str:
            return gr.update(value=f"⚠️ Error: Course Name and {document_type_str} content are required to send emails.")
        
        config_file_path = CONFIG_DIR / f"{course_name_str.replace(' ','_').lower()}_config.json"
        if not config_file_path.exists():
            return gr.update(value=f"⚠️ Error: Configuration for '{course_name_str}' not found.")
        
        cfg_data = json.loads(config_file_path.read_text(encoding="utf-8"))
        instructor_info = cfg_data.get("instructor", {})
        instructor_name = instructor_info.get("name", "Instructor")
        instructor_email = instructor_info.get("email")

        # Prepare DOCX attachment
        docx_buffer, docx_filename = download_docx(output_text_content_str, f"{course_name_str.replace(' ','_')}_{document_type_str.lower().replace(' ','_')}.docx")
        
        # Compile recipient list (instructor + students from text box)
        recipients_list = []
        if instructor_email:
            recipients_list.append({"name": instructor_name, "email": instructor_email})
        
        if students_text_input_str: # Students from the textbox in instructor UI
            for line_str in students_text_input_str.splitlines():
                if line_str.strip():
                    parts = [p.strip() for p in line_str.split(',', 1)] # Split only on first comma
                    if len(parts) == 2 and parts[0] and "@" in parts[1]:
                         recipients_list.append({"name": parts[0], "email": parts[1]})

        if not recipients_list:
            return gr.update(value="⚠️ Error: No valid recipients found (check instructor email in config and student list format).")

        successful_sends = 0
        error_messages_list = []
        
        email_subject = f"{document_type_str.capitalize()} for Course: {course_name_str}"
        email_body_html = f"""
        <html><body>
            <p>Dear Recipient,</p>
            <p>Please find attached the {document_type_str.lower()} for the course: <strong>{course_name_str}</strong>.</p>
            <p>This document was generated by the AI Tutor system.</p>
            <p>Best regards,<br>AI Tutor System ({instructor_name})</p>
        </body></html>"""

        for recipient in recipients_list:
            print(f"{status_message_prefix}: Preparing to send to {recipient['name']} <{recipient['email']}>...")
            # Pass the BytesIO buffer directly to send_email_notification
            # Ensure it's rewound if read previously, though download_docx returns it at position 0
            docx_buffer.seek(0) 
            if send_email_notification(
                to_email=recipient["email"], 
                subject=email_subject, 
                html_content=email_body_html,
                from_name=f"AI Tutor ({instructor_name})", # From display name
                attachment_file_obj=docx_buffer, # Pass the BytesIO object
                attachment_filename_override=docx_filename # Pass the desired filename
            ):
                successful_sends += 1
            else:
                error_messages_list.append(f"Failed to send to {recipient['email']}.")
        
        final_status_message = f"✅ {document_type_str.capitalize()} email attempts finished. Successfully sent to {successful_sends} out of {len(recipients_list)} recipient(s)."
        if error_messages_list:
            final_status_message += f"\n⚠️ Errors encountered:\n" + "\n".join(error_messages_list)
        return gr.update(value=final_status_message)

    except Exception as e:
        err_text = f"⚠️ Unexpected error during {status_message_prefix}:\n{e}\n{traceback.format_exc()}"
        print(err_text)
        return gr.update(value=err_text)

def email_syllabus_callback(c, s_str, out_content): return email_document_callback(c, "Syllabus", out_content, s_str)
def email_plan_callback(c, s_str, out_content): return email_document_callback(c, "Lesson Plan", out_content, s_str)


# --- Build Instructor UI ---
def build_instructor_ui():
    with gr.Blocks(theme=gr.themes.Soft(primary_hue=gr.themes.colors.blue, secondary_hue=gr.themes.colors.sky)) as instructor_panel_ui:
        gr.Markdown("## AI Tutor Instructor Panel")
        
        # Hidden state to store current course name for cross-tab use or persistence
        current_course_name_state = gr.State("")

        with gr.Tabs():
            # --- Tab 1: Course Setup & Syllabus ---
            with gr.TabItem("1. Course Setup & Syllabus", id="tab_setup"):
                gr.Markdown("### Create or Update Course Configuration and Generate Syllabus")
                with gr.Row():
                    course_name_input = gr.Textbox(label="Course Name*", placeholder="e.g., Introduction to Astrophysics")
                    instr_name_input = gr.Textbox(label="Instructor Name*", placeholder="e.g., Dr. Jane Doe")
                    instr_email_input = gr.Textbox(label="Instructor Email*", type="email", placeholder="e.g., jane.doe@example.edu")
                
                pdf_upload_component = gr.File(label="Upload Course Material PDF*", file_types=[".pdf"], type="file") # Use type="file" for SpooledTemporaryFile

                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("#### Course Schedule")
                        current_year = datetime.now().year
                        year_choices = [str(y) for y in range(current_year, current_year + 5)]
                        month_choices = [f"{m:02d}" for m in range(1, 13)]
                        day_choices = [f"{d:02d}" for d in range(1, 32)]
                        with gr.Row():
                            start_year_dd = gr.Dropdown(year_choices, label="Start Year*")
                            start_month_dd = gr.Dropdown(month_choices, label="Start Month*")
                            start_day_dd = gr.Dropdown(day_choices, label="Start Day*")
                        with gr.Row():
                            end_year_dd = gr.Dropdown(year_choices, label="End Year*")
                            end_month_dd = gr.Dropdown(month_choices, label="End Month*")
                            end_day_dd = gr.Dropdown(day_choices, label="End Day*")
                        class_days_cb_group = gr.CheckboxGroup(list(days_map.keys()), label="Class Days*", info="Select days of the week classes are held.")
                    
                    with gr.Column(scale=1):
                        gr.Markdown("#### Student & Access Details")
                        allowed_devices_cb_group = gr.CheckboxGroup(["Phone", "PC", "Tablet"], label="Allowed Devices for Tutor", value=["PC", "Tablet"])
                        students_textbox = gr.Textbox(
                            label="Student List (One per line: Name,Email)",
                            lines=5,
                            placeholder="Example One,student.one@example.com\nExample Two,student.two@example.com",
                            info="Enter student names and emails, separated by a comma."
                        )
                
                save_setup_button = gr.Button("1. Save Setup & Generate Syllabus", variant="primary", icon="💾")
                gr.Markdown("---")
                
                syllabus_output_textbox = gr.Textbox(label="Syllabus Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                
                with gr.Row(visible=False) as syllabus_actions_row_ui:
                    edit_syllabus_button = gr.Button(value="📝 Edit Syllabus Text")
                    email_syllabus_button = gr.Button(value="📧 Email Syllabus to All", variant="secondary")
                    # download_syllabus_button = gr.DownloadButton(label="📄 Download Syllabus as DOCX", visible=False) # Handled by email_document_callback

            # --- Tab 2: Lesson Plan Management ---
            with gr.TabItem("2. Lesson Plan Management", id="tab_plan"):
                lesson_plan_initial_message_md = gr.Markdown(
                    value="### Course Setup Required\nComplete 'Course Setup & Syllabus' on Tab 1 before generating a Lesson Plan here. The course name will appear below once setup is done.",
                    visible=True
                )
                # This textbox will be populated with the course name from Tab 1 after setup
                course_name_for_plan_display = gr.Textbox(label="Active Course for Lesson Plan", interactive=False, visible=False)
                
                generate_plan_button = gr.Button("2. Generate Lesson Plan", variant="primary", icon="📅", visible=False)
                
                lesson_plan_output_textbox = gr.Textbox(label="Lesson Plan Output", lines=20, interactive=False, visible=False, show_copy_button=True)
                
                with gr.Row(visible=False) as plan_actions_row_ui:
                    edit_plan_button = gr.Button(value="📝 Edit Plan Text")
                    email_plan_button = gr.Button(value="📧 Email Plan to All", variant="secondary")
                    # download_plan_button = gr.DownloadButton(label="📄 Download Plan as DOCX", visible=False)

            # --- Tab 3: Contact Support ---
            with gr.TabItem("Contact Support", id="tab_support"):
                gr.Markdown("### Send a Message to Support Team")
                with gr.Row():
                    contact_submitter_name = gr.Textbox(label="Your Name", placeholder="Enter your name")
                    contact_submitter_email = gr.Textbox(label="Your Email Address", type="email", placeholder="Enter your email")
                contact_message_body = gr.Textbox(label="Message Body", lines=7, placeholder="Describe your issue or query...")
                contact_file_attachment = gr.File(label="Attach File (Optional, e.g., screenshot)", file_count="single", type="file")
                send_contact_email_button = gr.Button("Send Message to Support", variant="primary", icon="✉️")
                contact_status_md = gr.Markdown(value="") # For displaying "Sent!" or "Failed."

                def handle_contact_form_submission(submitter_name, submitter_email, message_body, file_attachment):
                    validation_errors = []
                    if not submitter_name.strip(): validation_errors.append("Your Name is required.")
                    if not submitter_email.strip(): validation_errors.append("Your Email Address is required.")
                    elif "@" not in submitter_email: validation_errors.append("Please enter a valid Email Address.")
                    if not message_body.strip(): validation_errors.append("Message Body cannot be empty.")
                    
                    if validation_errors:
                        return (gr.update(value="<span style='color:red;'>Please fix errors:</span>\n" + "\n".join(f" - {err}" for err in validation_errors)),
                                submitter_name, submitter_email, message_body, file_attachment) # Return original values

                    support_email_address = os.getenv("SUPPORT_EMAIL_ADDRESS", "easyaitutor@gmail.com") # Configurable support email
                    email_subject = f"AI Tutor Support Request from: {submitter_name} <{submitter_email}>"
                    
                    # Sanitize message body for HTML email
                    html_message_body = f"<p><strong>From:</strong> {submitter_name} ({submitter_email})</p>"
                    html_message_body += "<p><strong>Message:</strong></p>"
                    html_message_body += f"<pre>{message_body.replace('<','&lt;').replace('>','&gt;')}</pre>" # Basic escaping

                    email_sent_successfully = send_email_notification(
                        to_email=support_email_address,
                        subject=email_subject,
                        html_content=html_message_body,
                        from_name=submitter_email, # Reply-To will be set to this
                        attachment_file_obj=file_attachment,
                        # attachment_filename_override=file_attachment.name if file_attachment else None # send_email_notification handles this
                    )
                    
                    if email_sent_successfully:
                        return (gr.update(value="<p style='color:green;'>Message sent successfully! ✔ We'll get back to you soon.</p>"),
                                gr.update(value=""), gr.update(value=""), gr.update(value=""), gr.update(value=None)) # Clear form
                    else:
                        return (gr.update(value="<p style='color:red;'>⚠️ Message could not be sent. Please check SMTP configuration or try again later.</p>"),
                                submitter_name, submitter_email, message_body, file_attachment) # Keep form values

                send_contact_email_button.click(
                    handle_contact_form_submission,
                    inputs=[contact_submitter_name, contact_submitter_email, contact_message_body, contact_file_attachment],
                    outputs=[contact_status_md, contact_submitter_name, contact_submitter_email, contact_message_body, contact_file_attachment],
                    queue=True # Use queue for potentially long operations like email sending
                )
        
        # --- Event Handling & Callbacks for Instructor Panel ---
        # These dummy buttons are not strictly necessary if all outputs are actual components
        # but can be useful if some .then() clauses need to output to non-UI elements or trigger other events.
        # For this structure, direct component updates are mostly used.
        # dummy_btn_1 = gr.Button(visible=False) 
        # dummy_btn_2 = gr.Button(visible=False)
        # dummy_btn_3 = gr.Button(visible=False)
        # dummy_btn_4 = gr.Button(visible=False)


        save_setup_button.click(
            save_setup,
            inputs=[
                course_name_input, instr_name_input, instr_email_input, allowed_devices_cb_group, pdf_upload_component,
                start_year_dd, start_month_dd, start_day_dd, end_year_dd, end_month_dd, end_day_dd,
                class_days_cb_group, students_textbox
            ],
            outputs=[ # Ensure this order matches the return tuple in save_setup
                syllabus_output_textbox, save_setup_button, # dummy_btn_1 (removed for now),
                generate_plan_button, edit_syllabus_button, email_syllabus_button,
                edit_plan_button, email_plan_button, syllabus_actions_row_ui,
                plan_actions_row_ui, lesson_plan_output_textbox, lesson_plan_initial_message_md,
                course_name_for_plan_display, current_course_name_state # Added state update
            ]
        ).then(
            lambda course_name_val: course_name_val, # Pass course name to state
            inputs=[course_name_input],
            outputs=[current_course_name_state]
        )
        
        # When course_name_input changes, update the display on Tab 2 and the state
        course_name_input.change(
            lambda val: (gr.update(value=val), val),
            inputs=[course_name_input],
            outputs=[course_name_for_plan_display, current_course_name_state]
        )


        edit_syllabus_button.click(
            enable_edit_syllabus_and_reload,
            inputs=[current_course_name_state, syllabus_output_textbox],
            outputs=[syllabus_output_textbox]
        )
        email_syllabus_button.click(
            email_syllabus_callback,
            inputs=[current_course_name_state, students_textbox, syllabus_output_textbox], # Use students_textbox for current student list
            outputs=[syllabus_output_textbox], # Show status in syllabus box
            queue=True
        )

        # Actions for Lesson Plan Tab
        generate_plan_button.click(
            generate_plan_callback,
            inputs=[current_course_name_state], # Use state for course name
            outputs=[ # Ensure this order matches return tuple in generate_plan_callback
                lesson_plan_output_textbox, # dummy_btn_2 (removed), dummy_btn_1 (removed),
                generate_plan_button, # dummy_btn_3 (removed), dummy_btn_4 (removed),
                edit_plan_button, email_plan_button
            ]
        ).then( # Ensure the plan output box and action row are visible after generation attempt
            lambda: (gr.update(visible=True), gr.update(visible=True)),
            inputs=None,
            outputs=[lesson_plan_output_textbox, plan_actions_row_ui]
        )
        
        edit_plan_button.click(
            enable_edit_plan_and_reload,
            inputs=[current_course_name_state, lesson_plan_output_textbox],
            outputs=[lesson_plan_output_textbox]
        )
        email_plan_button.click(
            email_plan_callback,
            inputs=[current_course_name_state, students_textbox, lesson_plan_output_textbox], # Use students_textbox
            outputs=[lesson_plan_output_textbox], # Show status in plan box
            queue=True
        )
        
    return instructor_panel_ui

# Mount Instructor UI
instructor_ui_instance = build_instructor_ui()
app = gr.mount_gradio_app(app, instructor_ui_instance, path="/instructor")

# Redirect root to instructor panel
@app.get("/")
def root_redirect():
    return RedirectResponse(url="/instructor")

# --- Student Tutor UI and Logic (Revised) ---
def build_student_tutor_ui():
    # OpenAI client will be initialized inside functions where needed or can be global
    # client = openai.OpenAI()

    def generate_student_system_prompt(mode, student_interests_str, current_topic, current_segment_text):
        # Ensure current_segment_text is not overly long for the prompt
        segment_preview = current_segment_text
        if len(current_segment_text) > 1000: # Max length for segment in prompt
            segment_preview = current_segment_text[:1000] + "..."

        base_prompt = (f"You are {STUDENT_BOT_NAME}, a friendly, patient, and encouraging AI English tutor. "
                       f"Your student's target English level is approximately {STUDENT_DEFAULT_ENGLISH_LEVEL}. "
                       f"Keep your responses concise, clear, and directly related to the student's input or the current learning mode. "
                       f"Always aim to be helpful and supportive.")

        if mode == "initial_greeting":
            return (f"{base_prompt} Today's lesson is about: '{current_topic}'. "
                    f"Let's start by getting to know each other a bit. What are some of your hobbies or interests?")
        elif mode == "onboarding":
            return (f"{base_prompt} You are continuing to get to know the student. "
                    f"Their interests mentioned so far include: {student_interests_str if student_interests_str else 'none yet'}. "
                    f"Ask another friendly, open-ended question to learn more about their preferences or daily life.")
        elif mode == "teaching_transition":
            return (f"{base_prompt} Student's interests: {student_interests_str if student_interests_str else 'various topics'}. "
                    f"Now, let's smoothly transition to our main topic for today: '{current_topic}'. "
                    f"This lesson is based on the following material: \"{segment_preview}\" "
                    f"To start, what do you already know or think about '{current_topic}'?")
        elif mode == "teaching":
            return (f"{base_prompt} You are currently teaching the student about '{current_topic}'. "
                    f"Refer to this text segment: \"{segment_preview}\". "
                    f"Relate to student interests ({student_interests_str if student_interests_str else 'general examples'}) if appropriate. "
                    f"Provide gentle corrections if they make mistakes. End your response with a question to encourage further interaction or check understanding.")
        elif mode == "interest_break_transition":
            return (f"{base_prompt} Great work so far! Let's take a very short break from '{current_topic}'. "
                    f"Thinking about your interests ({student_interests_str if student_interests_str else 'something fun'}), "
                    f"ask a light, engaging question or share a quick, interesting fact related to those interests.")
        elif mode == "interest_break_active":
            return (f"{base_prompt} The student has responded during the interest break. Give a brief, positive, and engaging reply. "
                    f"Then, gently guide the conversation back to the main lesson topic: '{current_topic}'.")
        elif mode == "quiz_time":
            return (f"{base_prompt} Alright, it's time for a quick quiz question on '{current_topic}'! "
                    f"Based on our discussion and this text: \"{segment_preview}\", "
                    f"I'll ask you a multiple-choice question. Please choose the best answer (A, B, or C).\n\n"
                    f"Internally, you MUST generate one clear multiple-choice question with three distinct options (A, B, C) and clearly identify the correct answer. Example for internal thought: 'Question: What is X? A) Opt1 B) Opt2 C) Opt3. Correct: B'. Present only the question and options to the student.")
        elif mode == "ending_session":
            return (f"{base_prompt} We're nearing the end of our session on '{current_topic}'. "
                    f"Thank you for your participation! Briefly summarize what was covered or offer a final encouraging thought.")
        elif mode == "error": # New mode for UI error state
            return (f"You are {STUDENT_BOT_NAME}. There has been an error loading the lesson. Apologize and ask the student to refresh or contact support.")
        return base_prompt # Default


    with gr.Blocks(theme=gr.themes.Soft(primary_hue=gr.themes.colors.teal, secondary_hue=gr.themes.colors.cyan)) as student_tutor_demo_ui:
        # States to hold dynamic lesson context, initialized by st_initialize_session
        st_token_val = gr.State(None)
        st_course_id_val = gr.State(None)
        st_lesson_id_val = gr.State(None)
        st_student_id_val = gr.State(None)
        st_lesson_topic_val = gr.State("Loading lesson...")
        st_lesson_segment_text_val = gr.State("Loading content...")
        
        # Standard session states
        st_chat_history_list = gr.State([])
        st_display_history_list = gr.State([])
        st_student_profile_dict = gr.State({"interests": [], "quiz_score": {"correct": 0, "total": 0, "last_question_correct_answer": None}})
        st_session_mode_str = gr.State("initial_greeting")
        st_turn_count_int = gr.State(0)
        st_teaching_turns_count_int = gr.State(0)
        st_session_start_time_dt = gr.State(None)

        # UI Components
        title_markdown = gr.Markdown(f"# {STUDENT_BOT_NAME}") # Will be updated
        # debug_info_markdown = gr.Markdown("Context: Loading...") # For dev, can be removed

        with gr.Row():
            with gr.Column(scale=1, min_width=200):
                voice_selector_dropdown = gr.Dropdown(choices=["alloy", "echo", "fable", "onyx", "nova", "shimmer"], value="nova", label="Tutor Voice")
                mic_audio_input = gr.Audio(sources=["microphone"], type="filepath", label="Record your response:", elem_id="student_mic_input")
                text_message_input = gr.Textbox(label="Or type your response here:", placeholder="Type and press Enter to send...", elem_id="student_text_input")
                send_message_button = gr.Button("Send Message", variant="primary", icon="💬")
            with gr.Column(scale=3, min_width=400):
                main_chatbot_interface = gr.Chatbot(label=f"Conversation with {STUDENT_BOT_NAME}", height=550, bubble_full_width=False, show_copy_button=True)
                tutor_audio_output = gr.Audio(type="filepath", autoplay=False, label=f"{STUDENT_BOT_NAME} says:", elem_id="tutor_audio_player")

        # Function to initialize the session using token from URL
        def st_initialize_session(request: gr.Request):
            token_from_url_params = request.query_params.get("token")
            initial_updates = { # Prepare a dictionary for all UI/state updates
                title_markdown: gr.update(value=f"# {STUDENT_BOT_NAME} - Error"),
                # debug_info_markdown: gr.update(value="Error loading lesson."),
                st_lesson_topic_val: "Error", st_lesson_segment_text_val: "Error loading",
                st_session_mode_str: "error", st_session_start_time_dt: datetime.now(dt_timezone.utc),
                main_chatbot_interface: [[None, "Error: Could not initialize lesson. Please check the URL or contact support."]],
                st_chat_history_list: [], st_display_history_list: [[None, "Error."]]
            }

            if not token_from_url_params:
                print("STUDENT_TUTOR_INIT: FATAL - No access token in URL.")
                initial_updates[main_chatbot_interface] = [[None, "Error: Access token missing. Please use the link provided in your email."]]
                initial_updates[st_display_history_list] = [[None, "Error: Access token missing."]]
                return initial_updates

            try:
                payload = jwt.decode(token_from_url_params, JWT_SECRET_KEY, algorithms=[ALGORITHM], audience=APP_DOMAIN)
                s_id, c_id, l_id = payload["sub"], payload["course_id"], int(payload["lesson_id"])

                config_path = CONFIG_DIR / f"{c_id.replace(' ','_').lower()}_config.json"
                if not config_path.exists(): raise FileNotFoundError("Course configuration not found for this lesson.")
                
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                full_text, lessons_data = cfg.get("full_text_content", ""), cfg.get("lessons", [])

                if not full_text or not lessons_data: raise ValueError("Lesson content or plan is missing in the course configuration.")
                if not (1 <= l_id <= len(lessons_data)): raise ValueError(f"Lesson ID {l_id} is out of range for this course.")

                l_topic = lessons_data[l_id - 1].get("topic_summary", f"Lesson {l_id}")
                
                # Calculate text segment for this specific lesson
                num_total_lessons = len(lessons_data)
                chars_per_segment = len(full_text) // num_total_lessons if num_total_lessons > 0 else len(full_text)
                start_char_index = (l_id - 1) * chars_per_segment
                end_char_index = l_id * chars_per_segment if l_id < num_total_lessons else len(full_text)
                l_segment_text = full_text[start_char_index:end_char_index].strip()
                if not l_segment_text: l_segment_text = "(No specific text segment for this lesson, focusing on general topic review.)"
                
                # Generate initial tutor message
                client = openai.OpenAI()
                initial_system_prompt = generate_student_system_prompt("initial_greeting", "", l_topic, l_segment_text)
                llm_response = client.chat.completions.create(
                    model=STUDENT_CHAT_MODEL,
                    messages=[{"role": "system", "content": initial_system_prompt}],
                    max_tokens=150, temperature=0.7
                )
                initial_tutor_msg_text = llm_response.choices[0].message.content.strip()
                
                current_chat_hist = [{"role": "assistant", "content": initial_tutor_msg_text}]
                current_display_hist = [[None, initial_tutor_msg_text]]
                
                tts_audio_path_update = None
                try:
                    tts_response_obj = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice="nova", input=initial_tutor_msg_text) # Default voice for greeting
                    intro_audio_filepath = STUDENT_AUDIO_DIR / f"init_greeting_{uuid.uuid4()}.mp3"
                    tts_response_obj.stream_to_file(intro_audio_filepath)
                    tts_audio_path_update = gr.update(value=str(intro_audio_filepath), autoplay=True)
                except Exception as e_tts_init:
                    print(f"STUDENT_TUTOR_INIT: TTS for initial greeting failed: {e_tts_init}")

                initial_updates.update({
                    title_markdown: gr.update(value=f"# {STUDENT_BOT_NAME} - Lesson: {l_topic}"),
                    # debug_info_markdown: gr.update(value=f"Course: {c_id}, Lesson: {l_id}, Student: {s_id}"),
                    st_token_val: token_from_url_params, st_course_id_val: c_id, st_lesson_id_val: l_id, st_student_id_val: s_id,
                    st_lesson_topic_val: l_topic, st_lesson_segment_text_val: l_segment_text,
                    st_chat_history_list: current_chat_hist, st_display_history_list: current_display_hist,
                    main_chatbot_interface: gr.update(value=current_display_hist),
                    tutor_audio_output: tts_audio_path_update,
                    st_session_mode_str: "onboarding", # Next mode after greeting
                    st_turn_count_int: 0, st_teaching_turns_count_int: 0,
                    st_session_start_time_dt: datetime.now(dt_timezone.utc)
                })
                return initial_updates

            except jwt.ExpiredSignatureError: error_text = "Access token has expired. Please use a new link from a recent email."
            except jwt.InvalidTokenError: error_text = "Invalid access token. Please ensure you're using the correct link."
            except FileNotFoundError as e_fnf: error_text = f"Course data error: {e_fnf}. Please contact support."
            except ValueError as e_val: error_text = f"Lesson data error: {e_val}. Please contact support."
            except openai.APIError as e_oai:
                error_text = f"AI service unavailable during setup: {e_oai}. Please try refreshing in a moment."
                print(f"STUDENT_TUTOR_INIT: OpenAI API Error: {e_oai}\n{traceback.format_exc()}")
            except Exception as e_gen:
                error_text = f"An unexpected error occurred while loading your lesson: {e_gen}. Please refresh or contact support."
                print(f"STUDENT_TUTOR_INIT: General Exception: {e_gen}\n{traceback.format_exc()}")
            
            initial_updates[main_chatbot_interface] = [[None, error_text]]
            initial_updates[st_display_history_list] = [[None, error_text]]
            # initial_updates[debug_info_markdown] = gr.update(value=error_text)
            return initial_updates

        # Process student's turn (speech or text)
        def st_process_student_turn(mic_filepath, typed_input_text, 
                                    current_chat_hist, current_display_hist, current_profile, 
                                    current_mode, current_turns, current_teaching_turns, 
                                    selected_tutor_voice,
                                    active_s_id, active_c_id, active_l_id, 
                                    active_l_topic, active_l_segment_text,
                                    session_start_dt):
            
            if current_mode == "error": # If session init failed, don't process further
                return current_display_hist, current_chat_hist, current_profile, current_mode, \
                       current_turns, current_teaching_turns, gr.update(value=None), \
                       gr.update(value=None), gr.update(value="") # Clear inputs

            student_input_text = ""
            client = openai.OpenAI() # Initialize client for this turn
            if mic_filepath:
                try:
                    with open(mic_filepath, "rb") as audio_file_obj:
                        transcription_response = client.audio.transcriptions.create(file=audio_file_obj, model=STUDENT_WHISPER_MODEL)
                    student_input_text = transcription_response.text.strip()
                    if not student_input_text: student_input_text = "(No speech detected in audio)"
                except Exception as e_stt:
                    student_input_text = f"(Audio transcription error: {e_stt})"
                    print(f"STUDENT_TUTOR_STT_ERROR: {e_stt}")
                try: os.remove(mic_filepath) # Clean up temp audio file
                except OSError: pass # Ignore if already removed or permission issue
            elif typed_input_text:
                student_input_text = typed_input_text.strip()
            
            if not student_input_text: # If no input after processing, return current state
                 return current_display_hist, current_chat_hist, current_profile, current_mode, \
                        current_turns, current_teaching_turns, gr.update(value=None), \
                        gr.update(value=None), gr.update(value="")

            current_display_hist.append([student_input_text, None]) # Show user message immediately
            current_chat_hist.append({"role": "user", "content": student_input_text})
            current_turns += 1
            
            # --- Mode Logic ---
            next_session_mode = current_mode
            if current_mode == "onboarding":
                current_profile["interests"].append(student_input_text)
                if current_turns >= STUDENT_ONBOARDING_TURNS: next_session_mode = "teaching_transition"
            elif current_mode == "teaching_transition":
                next_session_mode = "teaching"
            elif current_mode == "teaching":
                current_teaching_turns += 1
                # Quiz check should be before interest break if thresholds overlap
                if current_teaching_turns > 0 and current_teaching_turns % STUDENT_QUIZ_AFTER_TURNS == 0:
                    next_session_mode = "quiz_time"
                elif current_teaching_turns > 0 and current_teaching_turns % STUDENT_TEACHING_TURNS_PER_BREAK == 0 :
                    next_session_mode = "interest_break_transition"
            elif current_mode == "interest_break_transition":
                next_session_mode = "interest_break_active"
            elif current_mode == "interest_break_active":
                next_session_mode = "teaching" # Return to teaching
            elif current_mode == "quiz_time": # Student has just answered a quiz question
                # Basic Quiz Answer Evaluation (Placeholder)
                # Assumes LLM provided correct answer in a parseable way in its *previous* turn (internal thought)
                # And student_profile['last_question_correct_answer'] was set.
                # This is a simplified model. A robust quiz needs better state for question/answer.
                current_profile["quiz_score"]["total"] += 1
                last_correct_ans = current_profile.get("last_question_correct_answer", "").upper()
                if last_correct_ans and last_correct_ans in student_input_text.upper(): # Very basic check
                     current_profile["quiz_score"]["correct"] += 1
                current_profile["last_question_correct_answer"] = None # Reset for next quiz
                next_session_mode = "teaching"

            if current_turns >= STUDENT_MAX_SESSION_TURNS:
                next_session_mode = "ending_session"

            # --- Generate Tutor Response ---
            interests_str_for_prompt = ", ".join(current_profile["interests"]) if current_profile["interests"] else "not yet specified"
            current_system_prompt = generate_student_system_prompt(next_session_mode, interests_str_for_prompt, active_l_topic, active_l_segment_text)
            
            tutor_response_text = "I'm processing your input..."
            try:
                messages_for_llm_api = [{"role": "system", "content": current_system_prompt}] + current_chat_hist
                llm_api_response = client.chat.completions.create(
                    model=STUDENT_CHAT_MODEL, messages=messages_for_llm_api, max_tokens=250, temperature=0.7
                )
                tutor_response_text = llm_api_response.choices[0].message.content.strip()

                # If it's quiz time, try to extract the correct answer from LLM's response
                # This relies on the LLM following the prompt to internally note the correct answer.
                if next_session_mode == "quiz_time":
                    # Example: LLM message might contain "Correct answer is B" or similar.
                    # This is a naive parsing attempt. Robust quiz logic is harder.
                    match_correct = re.search(r"[Cc]orrect [Aa]nswer:?\s*([A-Ca-c])", tutor_response_text)
                    if match_correct:
                        current_profile["last_question_correct_answer"] = match_correct.group(1).upper()
                        # Optionally, remove this internal thought from the text shown to student.
                        # tutor_response_text = re.sub(r"\([Cc]orrect [Aa]nswer:?\s*[A-Ca-c]\)", "", tutor_response_text).strip()
                    else: # LLM didn't provide it clearly
                        current_profile["last_question_correct_answer"] = None


            except openai.APIError as e_oai_chat:
                print(f"STUDENT_TUTOR: OpenAI chat completion API error: {e_oai_chat}")
                tutor_response_text = "I encountered a small issue with my thinking process. Could you please rephrase or try again?"
            except Exception as e_llm_gen:
                print(f"STUDENT_TUTOR: General error during LLM response generation: {e_llm_gen}")
                tutor_response_text = "Apologies, I'm having a bit of trouble right now. Let's try that again."

            current_chat_hist.append({"role": "assistant", "content": tutor_response_text})
            current_display_hist[-1][1] = tutor_response_text # Update tutor's part of the last display entry

            # --- TTS for Tutor Response ---
            tts_audio_path_update = None
            try:
                tts_response_obj = client.audio.speech.create(model=STUDENT_TTS_MODEL, voice=selected_tutor_voice, input=tutor_response_text)
                reply_audio_filepath = STUDENT_AUDIO_DIR / f"tutor_reply_{uuid.uuid4()}.mp3"
                tts_response_obj.stream_to_file(reply_audio_filepath)
                tts_audio_path_update = gr.update(value=str(reply_audio_filepath), autoplay=True)
            except Exception as e_tts_reply:
                print(f"STUDENT_TUTOR: TTS for tutor reply failed: {e_tts_reply}")

            # --- Session End Logic ---
            if next_session_mode == "ending_session":
                session_end_dt = datetime.now(dt_timezone.utc)
                duration_in_seconds = 0
                if session_start_dt: # Check if session_start_dt was set
                     duration_in_seconds = (session_end_dt - session_start_dt).total_seconds()
                
                quiz_score_display_str = f"{current_profile['quiz_score']['correct']}/{current_profile['quiz_score']['total']}"
                engagement_summary_notes = f"Interests captured: {interests_str_for_prompt if interests_str_for_prompt != 'not yet specified' else 'None'}. Total turns: {current_turns}."
                
                log_student_progress(active_s_id, active_c_id, active_l_id, 
                                     quiz_score_display_str, duration_in_seconds, 
                                     engagement_notes=engagement_summary_notes)
                print(f"STUDENT_TUTOR: Session ended for student {active_s_id}. Progress logged.")
                # TODO: Implement detailed instructor report generation (matplotlib, GPT summary) and email sending here.
                # This would be a call to a new function:
                # generate_and_email_instructor_report(active_s_id, active_c_id, active_l_id, cfg_data_for_course, session_data_dict)
                # where cfg_data_for_course contains instructor_email and session_data_dict has all metrics.

            return current_display_hist, current_chat_hist, current_profile, next_session_mode, \
                   current_turns, current_teaching_turns, tts_audio_path_update, \
                   gr.update(value=None), gr.update(value="") # Clear mic and text inputs

        # --- Event Listeners for Student UI ---
        student_tutor_demo_ui.load(
            fn=st_initialize_session, 
            inputs=None, # Uses gr.Request implicitly
            outputs=[ # Ensure order matches the keys in the dictionary returned by st_initialize_session
                title_markdown, # debug_info_markdown,
                st_token_val, st_course_id_val, st_lesson_id_val, st_student_id_val,
                st_lesson_topic_val, st_lesson_segment_text_val,
                st_chat_history_list, st_display_history_list, main_chatbot_interface, tutor_audio_output,
                st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int,
                st_session_start_time_dt
            ]
        )
        
        # Consolidate inputs for event handlers
        process_turn_inputs = [
            mic_audio_input, text_message_input,
            st_chat_history_list, st_display_history_list, st_student_profile_dict,
            st_session_mode_str, st_turn_count_int, st_teaching_turns_count_int,
            voice_selector_dropdown,
            st_student_id_val, st_course_id_val, st_lesson_id_val,
            st_lesson_topic_val, st_lesson_segment_text_val,
            st_session_start_time_dt
        ]
        process_turn_outputs = [
            main_chatbot_interface, st_chat_history_list, st_student_profile_dict, st_session_mode_str,
            st_turn_count_int, st_teaching_turns_count_int, tutor_audio_output,
            mic_audio_input, text_message_input # Clear inputs
        ]

        mic_audio_input.change(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")
        text_message_input.submit(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")
        send_message_button.click(fn=st_process_student_turn, inputs=process_turn_inputs, outputs=process_turn_outputs, show_progress="hidden")

    return student_tutor_demo_ui

# Mount Student UI (once at startup)
student_tutor_app_instance = build_student_tutor_ui()
app = gr.mount_gradio_app(app, student_tutor_app_instance, path=STUDENT_UI_PATH)


# FastAPI endpoint to load student lesson (now primarily a redirector)
@app.get("/class", response_class=HTMLResponse) # response_class is for direct HTML, RedirectResponse overrides
async def route_get_student_lesson_page(request: Request, token: str = None):
    if not token:
        return HTMLResponse("<h3>Error: Access token missing.</h3><p>Please use the unique link provided in your email. If the issue persists, contact support.</p>", status_code=400)

    try:
        # Quick validation of token before redirecting. Full validation happens in the Gradio app.
        # This checks expiration and basic structure.
        jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM], audience=APP_DOMAIN, options={"verify_exp": True})
    except jwt.ExpiredSignatureError:
        return HTMLResponse("<h3>Error: Your lesson access link has expired.</h3><p>Links are typically valid for a limited time on your class day. Please check for a newer email or contact your instructor if you believe this is an error.</p>", status_code=401)
    except jwt.InvalidTokenError:
        return HTMLResponse("<h3>Error: Invalid access link.</h3><p>The link you used appears to be malformed or incorrect. Please double-check the link from your email or contact support.</p>", status_code=401)
    except Exception as e:
        print(f"Unexpected error during token pre-validation for /class: {e}")
        return HTMLResponse("<h3>Error: Could not validate access.</h3><p>An unexpected issue occurred. Please try again, or if the problem continues, contact support.</p>", status_code=500)

    # Redirect to the Gradio student UI, passing the token as a query parameter
    student_ui_redirect_url = f"{STUDENT_UI_PATH}?token={token}"
    print(f"Redirecting student to: {student_ui_redirect_url}")
    return RedirectResponse(url=student_ui_redirect_url)


# Debug endpoint (optional)
@app.get("/debug/run_reminders_manually")
async def debug_trigger_reminders():
    print("DEBUG: Manually triggering send_daily_class_reminders()...")
    try:
        send_daily_class_reminders() # This will run with current UTC date
        return {"status": "ok", "message": "send_daily_class_reminders job executed."}
    except Exception as e:
        print(f"Error in manual trigger of reminders: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


# --- FastAPI App Startup & Shutdown Events for APScheduler ---
@app.on_event("startup")
async def startup_event_tasks():
    # Schedule daily class reminders
    scheduler.add_job(
        send_daily_class_reminders, 
        trigger=CronTrigger(hour=5, minute=50, timezone='UTC'), # e.g., 5:50 AM UTC
        id="daily_class_reminders_job", 
        name="Daily Student Class Reminders", 
        replace_existing=True,
        misfire_grace_time=300 # Allow 5 mins for misfires
    )
    # Schedule student progress checks
    scheduler.add_job(
        check_student_progress_and_notify_professor, 
        trigger=CronTrigger(hour=18, minute=0, timezone='UTC'), # e.g., 6 PM UTC
        id="student_progress_check_job", 
        name="Check Student Progress & Notify Instructor", 
        replace_existing=True,
        misfire_grace_time=300
    )
    
    if not scheduler.running:
        scheduler.start()
        print("APScheduler started with jobs:")
    else:
        print("APScheduler already running. Current jobs:")
    for job in scheduler.get_jobs():
        print(f"  - Job ID: {job.id}, Name: {job.name}, Next Run: {job.next_run_time}")

@app.on_event("shutdown")
async def shutdown_event_tasks():
    if scheduler.running:
        scheduler.shutdown()
        print("APScheduler shutdown.")

# --- Main execution (for running with uvicorn) ---
if __name__ == "__main__":
    import uvicorn
    print(f"Starting AI Tutor Application...")
    print(f"Instructor Panel will be available at: http://localhost:8000/instructor")
    print(f"Student access links (from email) will point to: http://localhost:8000/class?token=...")
    print(f"Student UI will be rendered at: {STUDENT_UI_PATH}")
    print(f"Ensure all environment variables (OPENAI_API_KEY, SMTP settings, JWT_SECRET_KEY, APP_DOMAIN) are correctly set.")
    
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    # The uvicorn.run call will block here.
    # APScheduler's lifecycle is managed by FastAPI startup/shutdown events.
