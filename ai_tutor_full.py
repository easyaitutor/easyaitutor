import os
import io
import json
import traceback
import re
from pathlib import Path
from datetime import datetime, timedelta

import openai
import gradio as gr
from docx import Document
import smtplib
from email.message import EmailMessage

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configuration
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)

# SMTP Configuration
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", 587))
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")

# Constants
days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
            "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

# PDF loader & Section Splitter
try:
    import fitz
    def split_sections(pdf_file):
        doc = fitz.open(pdf_file.name) if hasattr(pdf_file, "name") else fitz.open(
            stream=pdf_file.read(), filetype="pdf"
        )
        pages = [page.get_text() for page in doc]
        doc.close()
        headings = []
        for i, text in enumerate(pages):
            for m in re.finditer(r"(?m)^(?:CHAPTER|Cap[ií]tulo)\s+.*", text, re.IGNORECASE):
                headings.append({"page": i+1, "start": (i, m.start()), "title": m.group().strip()})
        headings.sort(key=lambda h: (h['page'], h['start'][1]))
        sections = []
        for idx, h in enumerate(headings):
            sp, so = h['start']
            ep, eo = (headings[idx+1]['start'] if idx+1 < len(headings)
                      else (len(pages)-1, len(pages[-1])))
            content = ''
            for p in range(sp, ep+1):
                txt = pages[p]
                snippet = txt[so:] if p == sp else (txt[:eo] if p == ep else txt)
                content += snippet + '\n'
            sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
        return sections
except ImportError:
    from PyPDF2 import PdfReader
    def split_sections(pdf_file):
        data = pdf_file.read()
        rdr = PdfReader(pdf_file.name if hasattr(pdf_file, 'name') else io.BytesIO(data))
        text = "\n".join(page.extract_text() or '' for page in rdr.pages)
        chunks = re.split(r'(?<=[.?!])\s+', text)
        return [
            {'title': f'Lesson {i//5+1}', 'content': ' '.join(chunks[i:i+5]).strip(), 'page': None}
            for i in range(0, len(chunks), 5)
        ]

# Helpers
def download_docx(content, filename):
    buf = io.BytesIO()
    doc = Document()
    for line in content.split("\n"):
        doc.add_paragraph(line)
    doc.save(buf)
    buf.seek(0)
    return buf, filename

def count_classes(sd, ed, wdays):
    cnt = 0
    cur = sd
    while cur <= ed:
        if cur.weekday() in wdays:
            cnt += 1
        cur += timedelta(days=1)
    return cnt

# Generate Syllabus
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

# Generate Lesson Plan by Week
def generate_plan_by_week(cfg):
    sd = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date()
    ed = datetime.strptime(cfg['end_date'], '%Y-%m-%d').date()
    wdays = {days_map[d] for d in cfg['class_days']}
    dates = []
    cur = sd
    while cur <= ed:
        if cur.weekday() in wdays:
            dates.append(cur)
        cur += timedelta(days=1)
    summaries = []
    for sec in cfg['sections']:
        resp = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Summarize this section in one clear sentence."},
                {"role":"user","content": sec['content']}
            ],
            temperature=0.7, max_tokens=60
        )
        summaries.append(resp.choices[0].message.content.strip())
    weeks = {}
    for i, dt in enumerate(dates):
        wno = dt.isocalendar()[1]
        summ = summaries[i] if i < len(summaries) else "Topic to be announced."
        pg = cfg['sections'][i]['page'] if i < len(cfg['sections']) else None
        weeks.setdefault(wno, []).append((i+1, dt, summ, pg))
    lines = []
    for wno in sorted(weeks):
        lines.append(f"## Week {wno}\n")
        for num, dt, summ, pg in weeks[wno]:
            dstr = dt.strftime('%B %d, %Y')
            pstr = f" (p. {pg})" if pg else ''
            lines.append(f"**Lesson {num} ({dstr}){pstr}:** {summ}")
        lines.append('')
    return "\n".join(lines)

# Callbacks

def save_setup(course_name, instr_name, instr_email, devices, pdf_file,
               sy, sm, sd, ey, em, ed, class_days, students):
    try:
        sections = split_sections(pdf_file)
        full = "\n\n".join(f"{s['title']}\n{s['content']}" for s in sections)
        r1 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate a concise course description."},
                {"role":"user","content": full}
            ]
        )
        description = r1.choices[0].message.content.strip()
        r2 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate 5–12 clear learning objectives."},
                {"role":"user","content": full}
            ]
        )
        objectives = [ln.strip(' -•') for ln in r2.choices[0].message.content.splitlines() if ln.strip()]
        cfg = {
            "course_name": course_name,
            "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days,
            "start_date": f"{sy}-{sm}-{sd}",
            "end_date": f"{ey}-{em}-{ed}",
            "sections": sections,
            "course_description": description,
            "learning_objectives": objectives
        }
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        syllabus = generate_syllabus(cfg)
        return (
            gr.update(value=syllabus, visible=True, interactive=False),  # output
            gr.update(visible=False),  # Save Setup
            gr.update(visible=False),  # Show Syllabus
            gr.update(visible=False),  # Show Lesson Plan
            gr.update(visible=True),   # Edit Syllabus
            gr.update(visible=True),   # Email Syllabus
            gr.update(visible=False),  # Edit Lesson Plan
            gr.update(visible=False)   # Email Lesson Plan
        )
    except Exception:
        err = f"⚠️ Error:\n{traceback.format_exc()}"
        return (
            gr.update(value=err, visible=True, interactive=False),
            gr.update(visible=True), *(gr.update(visible=False),)*6
        )


def show_syllabus_callback(course_name):
    try:
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        syllabus = generate_syllabus(cfg)
        has_plan = "lesson_plan" in cfg
        return (
            gr.update(value=syllabus, visible=True, interactive=False),
            gr.update(visible=False),  # Save Setup
            gr.update(visible=False),  # Show Syllabus
            gr.update(visible=has_plan),
            gr.update(visible=True),   # Edit Syllabus
            gr.update(visible=True),   # Email Syllabus
            gr.update(visible=False),  # Edit Lesson Plan
            gr.update(visible=False)   # Email Lesson Plan
        )
    except Exception:
        return (gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}", visible=True, interactive=False),) + (None,)*7


def generate_plan_callback(course_name, sy, sm, sd, ey, em, ed, class_days):
    try:
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        # generate once then memorize
        if "lesson_plan" not in cfg:
            plan = generate_plan_by_week(cfg)
            cfg["lesson_plan"] = plan
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            plan = cfg["lesson_plan"]
        return (
            gr.update(value=plan, visible=True, interactive=False),
            gr.update(visible=False),  # Save Setup
            gr.update(visible=False),  # Show Syllabus
            gr.update(visible=True),   # Show Lesson Plan
            gr.update(visible=False),  # Edit Syllabus
            gr.update(visible=False),  # Email Syllabus
            gr.update(visible=True),   # Edit Lesson Plan
            gr.update(visible=True)    # Email Lesson Plan
        )
    except Exception:
        err = f"⚠️ Error:\n{traceback.format_exc()}"
        return (gr.update(value=err, visible=True, interactive=False),) + (None,)*7


def enable_edit_syllabus():
    return gr.update(interactive=True)


def enable_edit_plan():
    return gr.update(interactive=True)


def email_syllabus_callback(course_name, instr_name, instr_email, students, output):
    try:
        buf, fn = download_docx(output, f"{course_name}_syllabus.docx")
        data = buf.read()
        recs = [(instr_name, instr_email)] + [
            (n.strip(), e.strip()) for ln in students.splitlines() if ',' in ln for n, e in [ln.split(',',1)]
        ]
        for n, e in recs:
            msg = EmailMessage()
            msg["Subject"] = f"Course Syllabus: {course_name}"
            msg["From"]    = SMTP_USER
            msg["To"]      = e
            msg.set_content(f"Hi {n},\n\nAttached is the syllabus for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=fn
            )
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return "✅ Syllabus emailed!"
    except Exception:
        return f"⚠️ Error:\n{traceback.format_exc()}"


def email_plan_callback(course_name, instr_name, instr_email, students, output):
    try:
        buf, fn = download_docx(output, f"{course_name}_lesson_plan.docx")
        data = buf.read()
        recs = [(instr_name, instr_email)] + [
            (n.strip(), e.strip()) for ln in students.splitlines() if ',' in ln for n, e in [ln.split(',',1)]
        ]
        for n, e in recs:
            msg = EmailMessage()
            msg["Subject"] = f"Lesson Plan: {course_name}"
            msg["From"]    = SMTP_USER
            msg["To"]      = e
            msg.set_content(f"Hello {n},\n\nAttached is the lesson plan for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocess
