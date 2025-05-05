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
    import fitz  # PyMuPDF
    def split_sections(pdf_file):
        doc = fitz.open(pdf_file.name) if hasattr(pdf_file, "name") else fitz.open(stream=pdf_file.read(), filetype="pdf")
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
            ep, eo = (headings[idx+1]['start'] if idx+1 < len(headings) else (len(pages)-1, len(pages[-1])))
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
        reader = PdfReader(pdf_file.name) if hasattr(pdf_file, "name") else PdfReader(io.BytesIO(pdf_file.read()))
        text = "\n".join(page.extract_text() or '' for page in reader.pages)
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
    doc.save(buf); buf.seek(0)
    return buf, filename

def count_classes(sd, ed, wdays):
    cnt, cur = 0, sd
    while cur <= ed:
        if cur.weekday() in wdays:
            cnt += 1
        cur += timedelta(days=1)
    return cnt

# Syllabus Generator
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
        '_' * 60
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

# Lesson Plan Generator
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
                {"role": "system", "content": "Summarize this section in one clear sentence."},
                {"role": "user",   "content": sec['content']}
            ],
            temperature=0.7, max_tokens=60
        )
        summaries.append(resp.choices[0].message.content.strip())
    weeks = {}
    for idx, dt in enumerate(dates):
        wno = dt.isocalendar()[1]
        summ = summaries[idx] if idx < len(summaries) else "Topic to be announced."
        pg = cfg['sections'][idx]['page'] if idx < len(cfg['sections']) else None
        weeks.setdefault(wno, []).append((idx+1, dt, summ, pg))
    out = []
    for wno in sorted(weeks):
        out.append(f"## Week {wno}\n")
        for num, dt, summ, pg in weeks[wno]:
            ds = dt.strftime('%B %d, %Y')
            pinfo = f" (p. {pg})" if pg else ''
            out.append(f"**Lesson {num} ({ds}){pinfo}:** {summ}")
        out.append('')
    return "\n".join(out)

# Callbacks
def save_setup(course_name, instr_name, instr_email, devices, pdf_file,
               sy, sm, sd, ey, em, ed, class_days, students):
    try:
        secs = split_sections(pdf_file)
        full = "\n\n".join(f"{s['title']}\n{s['content']}" for s in secs)
        r1 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Generate a concise course description."},
                {"role": "user",   "content": full}
            ]
        )
        desc = r1.choices[0].message.content.strip()
        r2 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Generate 5–12 clear learning objectives."},
                {"role": "user",   "content": full}
            ]
        )
        objs = [ln.strip(' -•') for ln in r2.choices[0].message.content.splitlines() if ln.strip()]
        cfg = {
            "course_name": course_name,
            "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days,
            "start_date": f"{sy}-{sm}-{sd}",
            "end_date":   f"{ey}-{em}-{ed}",
            "sections": secs,
            "course_description": desc,
            "learning_objectives": objs
        }
        p = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        syl = generate_syllabus(cfg)
        return (
            gr.update(value=syl, visible=True, interactive=False),  # output
            gr.update(visible=False),                                # Save Setup
            gr.update(visible=False),                                # Show Syllabus
            gr.update(visible=True),     # btn_plan
            gr.update(visible=True),                                 # Edit Syllabus
            gr.update(visible=True),                                 # Email Syllabus
            gr.update(visible=False),                                # Edit Lesson Plan
            gr.update(visible=False)                                 # Email Lesson Plan
        )
    except Exception:
        return (
            gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}", visible=True, interactive=False),
            gr.update(visible=True), *(gr.update(visible=False),)*6
        )

def show_syllabus_callback(course_name):
    try:
        p = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        syl = generate_syllabus(cfg)
        has_plan = "lesson_plan" in cfg
        return (
            gr.update(value=syl, visible=True, interactive=False),  # output
            gr.update(visible=False),                                # Save Setup
            gr.update(visible=False),                                # Show Syllabus
            gr.update(visible=has_plan),  # btn_plan
            gr.update(visible=True),                                 # Edit Syllabus
            gr.update(visible=True),                                 # Email Syllabus
            gr.update(visible=False),                                # Edit Lesson Plan
            gr.update(visible=False)                                 # Email Lesson Plan
        )
    except Exception:
        return (gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}", visible=True, interactive=False),) + (None,)*7

def generate_plan_callback(course_name, sy, sm, sd, ey, em, ed, class_days):
    try:
        cfg_path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Generate or retrieve existing lesson plan
        if "lesson_plan" not in cfg:
            plan = generate_plan_by_week(cfg)
            cfg["lesson_plan"] = plan
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            plan = cfg["lesson_plan"]
        return (
            gr.update(value=plan, visible=True, interactive=False),  # output
            gr.update(visible=False),                                # Save Setup
            gr.update(visible=False),                                # Show Syllabus
            gr.update(visible=True),                                 # Show Lesson Plan
            gr.update(visible=False),                                # Edit Syllabus
            gr.update(visible=False),                                # Email Syllabus
            gr.update(visible=True),                                 # Edit Lesson Plan
            gr.update(visible=True)                                  # Email Lesson Plan
        )
    except Exception:
        # Return error message
        return (
            gr.update(value=f"⚠️ Error:
{traceback.format_exc()}", visible=True, interactive=False),
            *(None,)*7
        )

def enable_edit_syllabus():():
    return gr.update(interactive=True)

def enable_edit_plan():
    return gr.update(interactive=True)

def email_syllabus_callback(course_name, instr_name, instr_email, students, output):
    try:
        buf, fn = download_docx(output, f"{course_name}_syllabus.docx")
        data = buf.read()
        recs = [(instr_name, instr_email)] + [(n.strip(), e.strip()) for ln in students.splitlines() if ',' in ln for n,e in [ln.split(',',1)]]
        for n,e in recs:
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
        recs = [(instr_name, instr_email)] + [(n.strip(), e.strip()) for ln in students.splitlines() if ',' in ln for n,e in [ln.split(',',1)]]
        for n,e in recs:
            msg = EmailMessage()
            msg["Subject"] = f"Lesson Plan: {course_name}"
            msg["From"]    = SMTP_USER
            msg["To"]      = e
            msg.set_content(f"Hello {n},\n\nAttached is the lesson plan for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=fn
            )
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return "✅ Lesson Plan emailed!"
    except Exception:
        return f"⚠️ Error:\n{traceback.format_exc()}"

# UI Construction
def build_ui():
    with gr.Blocks() as demo:
        gr.Markdown("## AI Tutor Instructor Panel")
        with gr.Row():
            course = gr.Textbox(label="Course Name")
            instr  = gr.Textbox(label="Instructor Name")
            email  = gr.Textbox(label="Instructor Email")
        devices  = gr.CheckboxGroup(["phone","pc"], label="Allowed Devices")
        pdf_file = gr.File(label="Upload PDF (.pdf)", file_types=[".pdf"])
        years  = [str(y) for y in range(2023,2031)]
        months = [f"{m:02d}" for m in range(1,13)]
        days   = [f"{d:02d}" for d in range(1,32)]
        with gr.Row():
            sy = gr.Dropdown(years, label="Start Year")
            sm = gr.Dropdown(months, label="Start Month")
            sd = gr.Dropdown(days,   label="Start Day")
        with gr.Row():
            ey = gr.Dropdown(years, label="End Year")
            em = gr.Dropdown(months, label="End Month")
            ed = gr.Dropdown(days,   label="End Day")
        class_days = gr.CheckboxGroup(list(days_map.keys()), label="Class Days")
        students   = gr.Textbox(label="Students (Name,Email per line)", lines=4)
        output_box = gr.Textbox(label="Output", lines=30, interactive=False, visible=False)
        
        # Buttons
        btn_save           = gr.Button("Save Setup")
        btn_show           = gr.Button("Show Syllabus", visible=False)
        btn_plan           = gr.Button("Show Lesson Plan", visible=False)
        btn_edit_syllabus  = gr.Button("Edit Syllabus", visible=False)
        btn_email_syllabus = gr.Button("Email Syllabus", visible=False)
        btn_edit_plan      = gr.Button("Edit Lesson Plan", visible=False)
        btn_email_plan     = gr.Button("Email Lesson Plan", visible=False)

        # Wiring
        btn_save.click(
            save_setup,
            inputs=[course,instr,email,devices,pdf_file,sy,sm,sd,ey,em,ed,class_days,students],
            outputs=[output_box,btn_save,btn_show,btn_plan,btn_edit_syllabus,btn_email_syllabus,btn_edit_plan,btn_email_plan]
        )
        btn_show.click(
            show_syllabus_callback,
            inputs=[course],
            outputs=[output_box,btn_save,btn_show,btn_plan,btn_edit_syllabus,btn_email_syllabus,btn_edit_plan,btn_email_plan]
        )
        btn_plan.click(
            generate_plan_callback,
            inputs=[course,sy,sm,sd,ey,em,ed,class_days],
            outputs=[output_box,btn_save,btn_show,btn_plan,btn_edit_syllabus,btn_email_syllabus,btn_edit_plan,btn_email_plan]
        )
        btn_edit_syllabus.click(
            enable_edit_syllabus, [], [output_box]
        )
        btn_edit_plan.click(
            enable_edit_plan, [], [output_box]
        )
        btn_email_syllabus.click(
            email_syllabus_callback, [course,instr,email,students,output_box], [output_box]
        )
        btn_email_plan.click(
            email_plan_callback, [course,instr,email,students,output_box], [output_box]
        )
    return demo

# FastAPI Mount
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

if __name__ == "__main__":
    build_ui().launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT",7860)))
