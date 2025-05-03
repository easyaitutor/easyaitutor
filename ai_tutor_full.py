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

# ——— Configuration ———
openai.api_key = os.getenv("OPENAI_API_KEY")
CONFIG_DIR = Path("course_data")
CONFIG_DIR.mkdir(exist_ok=True)

# ——— SMTP Configuration ———
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", 587))
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")

# ——— Constants ———
days_map = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2,
    "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6
}

# ——— PDF loader ———
try:
    import fitz  # PyMuPDF
    def load_pdf_text(f):
        doc = fitz.open(f.name) if hasattr(f, "name") else fitz.open(
            stream=f.read(), filetype="pdf"
        )
        text = "".join(page.get_text() + "\n" for page in doc)
        doc.close()
        return text
except ImportError:
    from PyPDF2 import PdfReader
    def load_pdf_text(f):
        if hasattr(f, "name"):
            reader = PdfReader(f.name)
        else:
            tmp = Path("tmp_course.pdf"); tmp.write_bytes(f.read())
            reader = PdfReader(str(tmp))
        return "".join(p.extract_text() + "\n" for p in reader.pages)

# ——— Helpers ———
def split_sections(pdf_file):
    text = load_pdf_text(pdf_file)
    headings = list(re.finditer(r"(?m)^(?:CHAPTER|Cap[ií]tulo)\s+.*", text, re.IGNORECASE))
    if headings:
        sections = []
        for i, h in enumerate(headings):
            start = h.end()
            end = headings[i+1].start() if i+1 < len(headings) else len(text)
            sections.append({"title": h.group().strip(), "content": text[start:end].strip()})
        return sections
    chunks = re.split(r'(?<=[.?!])\s+', text)
    return [{"title": f"Lesson {i//5+1}", "content": " ".join(chunks[i:i+5]).strip()}
            for i in range(0, len(chunks), 5)]

def count_classes(start_date, end_date, weekdays):
    cnt, cur = 0, start_date
    while cur <= end_date:
        if cur.weekday() in weekdays:
            cnt += 1
        cur += timedelta(days=1)
    return cnt

def generate_syllabus(cfg):
    sd = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date()
    ed = datetime.strptime(cfg['end_date'],   '%Y-%m-%d').date()
    month_range = f"{sd.strftime('%B')}–{ed.strftime('%B')}"
    total = count_classes(sd, ed, [days_map[d] for d in cfg['class_days']])
    header = [
        f"Course Name: {cfg['course_name']}",
        f"Professor:   {cfg['instructor']['name']}",
        f"Email:       {cfg['instructor']['email']}",
        f"Duration:    {month_range} ({total} classes)",
        "_" * 60
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
        f" • {month_range}, every {', '.join(cfg['class_days'])}",
        "", "OFFICE HOURS & SUPPORT:",
        " • Office Hours: Tuesdays 3–5 PM; Thursdays 10–11 AM (Zoom)",
        " • Email response within 24 hours on weekdays"
    ]
    return "\n".join(header + [""] + body)

# ——— Callbacks ———
def save_setup(course_name, instr_name, instr_email, devices, pdf_file,
               sy, sm, sd, ey, em, ed, class_days, students):
    try:
        sections = split_sections(pdf_file)
        full_text = "\n\n".join(f"{s['title']}\n{s['content']}" for s in sections)
        desc = openai.ChatCompletion.create(
            model='gpt-3.5-turbo',
            messages=[
                {'role':'system','content':'Generate a concise course description.'},
                {'role':'user','content': full_text}
            ],
            max_tokens=200
        ).choices[0].message.content.strip()
        obj = openai.ChatCompletion.create(
            model='gpt-3.5-turbo',
            messages=[
                {'role':'system','content':'Generate 5–12 clear learning objectives.'},
                {'role':'user','content': full_text}
            ],
            max_tokens=400
        ).choices[0].message.content.splitlines()
        objectives = [ln.strip('-• ').strip() for ln in obj if ln.strip()]

        cfg = {
            'course_name': course_name,
            'instructor':  {'name': instr_name, 'email': instr_email},
            'class_days':   class_days,
            'start_date':   f"{sy}-{sm}-{sd}",
            'end_date':     f"{ey}-{em}-{ed}",
            'sections':     sections,
            'course_description':    desc,
            'learning_objectives':   objectives
        }
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        syllabus = generate_syllabus(cfg)
        return (
            gr.update(value=syllabus, visible=True, interactive=False),
            gr.update(visible=False), gr.update(visible=True),
            gr.update(visible=True), gr.update(visible=True)
        )
    except Exception:
        return (f"⚠️ Error:\n{traceback.format_exc()}",) + (None,)*4

def enable_edit():
    return gr.update(interactive=True)

def download_syllabus(course_name, text):
    buf = io.BytesIO()
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(buf); buf.seek(0)
    fn = f"{course_name.replace(' ','_').lower()}_syllabus.docx"
    return buf, fn

def email_syllabus_callback(course_name, instr_name, instr_email, students_text, syllabus_text):
    try:
        buf, fn = download_syllabus(course_name, syllabus_text)
        data = buf.read()
        recipients = [(instr_name, instr_email)]
        for line in students_text.splitlines():
            if ',' in line:
                n, e = line.split(',',1); recipients.append((n.strip(), e.strip()))
        for n, e in recipients:
            msg = EmailMessage()
            msg['Subject'] = f"Course Syllabus: {course_name}"
            msg['From']    = SMTP_USER
            msg['To']      = e
            msg.set_content(f"Hi {n},\n\nPlease find attached the syllabus for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype='application',
                subtype='vnd.openxmlformats-officedocument.wordprocessingml.document',
                filename=fn
            )
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        return gr.update(value="Emails sent successfully!", visible=True)
    except Exception:
        return gr.update(value=f"⚠️ Email error:\n{traceback.format_exc()}", visible=True)

# ——— Build the Gradio UI ———
def build_ui():
    with gr.Blocks() as demo:
        gr.Markdown("## AI Tutor Instructor Panel")
        with gr.Row():
            course = gr.Textbox(label="Course Name")
            instr  = gr.Textbox(label="Instructor Name")
            email  = gr.Textbox(label="Instructor Email")
        devices  = gr.CheckboxGroup(["phone","pc"], label="Allowed Devices")
        pdf_file = gr.File(label="Upload PDF (.pdf)", file_types=[".pdf"])
        years    = [str(y) for y in range(2023,2031)]
        months   = [f"{m:02d}" for m in range(1,13)]
        days     = [f"{d:02d}" for d in range(1,32)]
        with gr.Row():
            sy, sm, sd = gr.Dropdown(years, label="Start Year"), gr.Dropdown(months, label="Start Month"), gr.Dropdown(days, label="Start Day")
        with gr.Row():
            ey, em, ed = gr.Dropdown(years, label="End Year"),   gr.Dropdown(months, label="End Month"),   gr.Dropdown(days, label="End Day")
        class_days = gr.CheckboxGroup(list(days_map.keys()), label="Class Days")
        students   = gr.Textbox(label="Students (Name,Email per line)", lines=4)
        output     = gr.Textbox(label="Syllabus Preview", lines=30, interactive=False, visible=False)
        status     = gr.Textbox(label="Email Status", lines=2, interactive=False, visible=False)
        btn_save   = gr.Button("Save Setup")
        btn_edit   = gr.Button("Edit", visible=False)
        btn_email  = gr.Button("Email Syllabus", visible=False)

        btn_save.click(save_setup, [course,instr,email,devices,pdf_file,sy,sm,sd,ey,em,ed,class_days,students],
                                      [output,btn_save,btn_edit,btn_email,status])
        btn_edit.click(enable_edit, [], [output])
        btn_email.click(email_syllabus_callback, [course,instr,email,students,output], [status])
    return demo

# ——— FastAPI + Gradio mounting ———
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"],
)

gradio_app = build_ui()
app = gr.mount_gradio_app(app, gradio_app, path="/")

@app.get("/healthz")
def healthz():
    return {"status":"ok"}

# ——— Local dev entrypoint ———
if __name__ == "__main__":
    build_ui().launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT", 7860)))
