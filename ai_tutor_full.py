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
days_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
            "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}

# ——— PDF loader & Section Splitter (with page numbers) ———
try:
    import fitz  # PyMuPDF
    def split_sections(pdf_file):
        doc = fitz.open(pdf_file.name) if hasattr(pdf_file, "name") else fitz.open(stream=pdf_file.read(), filetype="pdf")
        pages_text = [page.get_text() for page in doc]
        doc.close()
        headings = []
        for p_i, text in enumerate(pages_text):
            for m in re.finditer(r"(?m)^(?:CHAPTER|Cap[ií]tulo)\s+.*", text, re.IGNORECASE):
                headings.append({'page': p_i+1, 'start': (p_i, m.start()), 'title': m.group().strip()})
        headings.sort(key=lambda h: (h['page'], h['start'][1]))
        sections = []
        for i, h in enumerate(headings):
            start_p, start_off = h['start']
            end_p, end_off = (headings[i+1]['start'] if i+1 < len(headings) else (len(pages_text)-1, len(pages_text[-1])))
            content = ''
            for p in range(start_p, end_p+1):
                txt = pages_text[p]
                snippet = txt[start_off:] if p == start_p else (txt[:end_off] if p == end_p else txt)
                content += snippet + '\n'
            sections.append({'title': h['title'], 'content': content.strip(), 'page': h['page']})
        return sections
except ImportError:
    from PyPDF2 import PdfReader
    def split_sections(pdf_file):
        reader = PdfReader(pdf_file.name) if hasattr(pdf_file, "name") else PdfReader(io.BytesIO(pdf_file.read()))
        text = "\n".join(p.extract_text() or '' for p in reader.pages)
        chunks = re.split(r'(?<=[.?!])\s+', text)
        return [{'title': f'Lesson {i//5+1}', 'content': ' '.join(chunks[i:i+5]).strip(), 'page': None} for i in range(0, len(chunks), 5)]

# ——— Syllabus Generator ———
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
        f" • {month_range}, every {', '.join(cfg['class_days'])}",
        "", "OFFICE HOURS & SUPPORT:",
        " • Office Hours: Tuesdays 3–5 PM; Thursdays 10–11 AM (Zoom)",
        " • Email response within 24 hours on weekdays"
    ]
    return "\n".join(header + [''] + body)

# ——— Lesson Plan Generator (grouped by week) ———
def generate_plan_by_week(cfg):
    sd = datetime.strptime(cfg['start_date'], '%Y-%m-%d').date()
    ed = datetime.strptime(cfg['end_date'],   '%Y-%m-%d').date()
    weekdays = {days_map[d] for d in cfg['class_days']}
    dates = []
    cur = sd
    while cur <= ed:
        if cur.weekday() in weekdays:
            dates.append(cur)
        cur += timedelta(days=1)

    summaries = []
    for sec in cfg['sections']:
        resp = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Summarize this course section in one clear sentence describing the teaching focus."},
                {"role":"user","content": sec['content']}
            ],
            temperature=0.7,
            max_tokens=60
        )
        summaries.append(resp.choices[0].message.content.strip())

    weeks = {}
    for idx, date in enumerate(dates):
        week_no = date.isocalendar()[1]
        summary = summaries[idx] if idx < len(summaries) else 'Topic to be announced.'
        page = cfg['sections'][idx]['page'] if idx < len(cfg['sections']) else None
        weeks.setdefault(week_no, []).append((idx+1, date, summary, page))

    lines = []
    for wno in sorted(weeks):
        lines.append(f"## Week {wno}\n")
        for num, date, summ, page in weeks[wno]:
            date_str = date.strftime('%B %d, %Y')
            pg = f" (p. {page})" if page else ''
            lines.append(f"**Lesson {num} ({date_str}){pg}:** {summ}")
        lines.append('')
    return "\n".join(lines)

# ——— Callbacks ———
def save_setup(course_name, instr_name, instr_email, devices, pdf_file,
               sy, sm, sd, ey, em, ed, class_days, students):
    try:
        sections = split_sections(pdf_file)
        full_text = "\n\n".join(f"{s['title']}\n{s['content']}" for s in sections)
        # description
        resp = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate a concise course description."},
                {"role":"user","content": full_text}
            ]
        )
        desc = resp.choices[0].message.content.strip()
        # objectives
        resp2 = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Generate five to twelve clear learning objectives."},
                {"role":"user","content": full_text}
            ]
        )
        obj_lines = resp2.choices[0].message.content.splitlines()
        objectives = [ln.strip(' -•') for ln in obj_lines if ln.strip()]
        cfg = {
            "course_name": course_name,
            "instructor": {"name": instr_name, "email": instr_email},
            "class_days": class_days,
            "start_date": f"{sy}-{sm}-{sd}",
            "end_date":   f"{ey}-{em}-{ed}",
            "sections": sections,
            "course_description": desc,
            "learning_objectives": objectives
        }
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        syllabus = generate_syllabus(cfg)
        # after save: show toolbar for show & generate
        return (
            gr.update(value=syllabus, interactive=False),  # output_box
            gr.update(visible=False),  # btn_save
            gr.update(visible=True),   # btn_show_syllabus
            gr.update(visible=True),   # btn_gen_plan
            gr.update(visible=False),  # btn_edit_syllabus
            gr.update(visible=False),  # btn_email_syllabus
            gr.update(visible=False),  # btn_edit_plan
            gr.update(visible=False)   # btn_email_plan
        )
    except Exception:
        err = f"⚠️ Error:\n{traceback.format_exc()}"
        return (
            gr.update(value=err, interactive=False),
            gr.update(visible=True),  # btn_save
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(visible=False)
        )

def show_syllabus_callback(course_name):
    try:
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        syllabus = generate_syllabus(cfg)
        return (
            gr.update(value=syllabus, interactive=False),  # output_box
            None, None,                           # btn_save, btn_show_syllabus
            gr.update(visible=True),              # btn_edit_syllabus
            gr.update(visible=True),              # btn_email_syllabus
            gr.update(visible=False),             # btn_edit_plan
            gr.update(visible=False)              # btn_email_plan
        )
    except Exception:
        return (
            gr.update(value=f"⚠️ Error loading syllabus:\n{traceback.format_exc()}", interactive=False),
            None, None, None, None, None, None, None
        )

def enable_edit_syllabus():
    return (
        gr.update(interactive=True),  # output_box
        None, None, None, None, None, None, None
    )

def generate_plan_callback(course_name, sy, sm, sd, ey, em, ed, class_days):
    try:
        path = CONFIG_DIR / f"{course_name.replace(' ','_').lower()}_config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        plan = generate_plan_by_week(cfg)
        return (
            gr.update(value=plan, interactive=False),  # output_box
            None, None,                           # btn_save, btn_show_syllabus
            gr.update(visible=False),             # btn_edit_syllabus
            gr.update(visible=False),             # btn_email_syllabus
            gr.update(visible=True),              # btn_edit_plan
            gr.update(visible=True)               # btn_email_plan
        )
    except Exception:
        return (
            gr.update(value=f"⚠️ Error generating plan:\n{traceback.format_exc()}", interactive=False),
            None, None, None, None, None, None, None
        )

def enable_edit_plan():
    return (
        gr.update(interactive=True),  # output_box
        None, None, None, None, None, None, None
    )

def email_syllabus_callback(course_name, instr_name, instr_email, students_text, output_text):
    try:
        buf, fn = download_docx(output_text, f"{course_name}_syllabus.docx")
        data = buf.read()
        recipients = [(instr_name, instr_email)]
        for ln in students_text.splitlines():
            if ',' in ln:
                n,e = ln.split(',',1)
                recipients.append((n.strip(), e.strip()))
        for n,e in recipients:
            msg = EmailMessage()
            msg["Subject"] = f"Course Syllabus: {course_name}"
            msg["From"]    = SMTP_USER
            msg["To"]      = e
            msg.set_content(f"Hi {n},\n\nAttached is the syllabus for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=fn
            )
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return (
            gr.update(value="✅ Syllabus emailed!", interactive=False),
            None, None, None,
            None, None,
            None, None
        )
    except Exception:
        return (
            gr.update(value=f"⚠️ Email error:\n{traceback.format_exc()}", interactive=False),
            None, None, None, None, None, None, None
        )

def email_plan_callback(course_name, instr_name, instr_email, students_text, output_text):
    try:
        buf, fn = download_docx(output_text, f"{course_name}_lesson_plan.docx")
        data = buf.read()
        recipients = [(instr_name, instr_email)]
        for ln in students_text.splitlines():
            if ',' in ln:
                n,e = ln.split(',',1)
                recipients.append((n.strip(), e.strip()))
        for n,e in recipients:
            msg = EmailMessage()
            msg["Subject"] = f"Lesson Plan: {course_name}"
            msg["From"]    = SMTP_USER
            msg["To"]      = e
            msg.set_content(f"Hello {n},\n\nAttached is the lesson plan for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(
                data,
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=fn
            )
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return (
            gr.update(value="✅ Lesson Plan emailed!", interactive=False),
            None, None, None, None,
            None, None,
            None, None
        )
    except Exception:
        return (
            gr.update(value=f"⚠️ Email error:\n{traceback.format_exc()}", interactive=False),
            None, None, None, None, None, None, None
        )

# ——— Build the Gradio UI & CSS ———
def build_ui():
    css = ".small-btn { padding: 0.25rem 0.5rem; font-size: 0.9rem; }"
    with gr.Blocks(css=css) as demo:
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
            sd = gr.Dropdown(days, label="Start Day")
        with gr.Row():
            ey = gr.Dropdown(years, label="End Year")
            em = gr.Dropdown(months, label="End Month")
            ed = gr.Dropdown(days, label="End Day")
        class_days = gr.CheckboxGroup(list(days_map.keys()), label="Class Days")
        students   = gr.Textbox(label="Students (Name,Email per line)", lines=4)

        output_box = gr.Textbox(label="Output", lines=30, interactive=False, visible=False)
        with gr.Row():
            btn_save          = gr.Button("💾", elem_classes=["small-btn"], tooltip="Save Setup")
            btn_show_syllabus = gr.Button("📖", elem_classes=["small-btn"], visible=False, tooltip="Show Syllabus")
            btn_gen_plan      = gr.Button("🗒️", elem_classes=["small-btn"], visible=False, tooltip="Generate Lesson Plan")
            btn_edit_syllabus = gr.Button("✏️", elem_classes=["small-btn"], visible=False, tooltip="Edit Syllabus")
            btn_email_syllabus= gr.Button("📧", elem_classes=["small-btn"], visible=False, tooltip="Email Syllabus")
            btn_edit_plan     = gr.Button("✏️", elem_classes=["small-btn"], visible=False, tooltip="Edit Plan")
            btn_email_plan    = gr.Button("📧", elem_classes=["small-btn"], visible=False, tooltip="Email Plan")

        # Wiring
        btn_save.click(
            save_setup,
            inputs=[course,instr,email,devices,pdf_file,sy,sm,sd,ey,em,ed,class_days,students],
            outputs=[output_box, btn_save, btn_show_syllabus, btn_gen_plan,
                     btn_edit_syllabus, btn_email_syllabus,
                     btn_edit_plan, btn_email_plan]
        )
        btn_show_syllabus.click(
            show_syllabus_callback,
            inputs=[course],
            outputs=[output_box, None, None, None,
                     btn_edit_syllabus, btn_email_syllabus,
                     btn_edit_plan, btn_email_plan]
        )
        btn_gen_plan.click(
            generate_plan_callback,
            inputs=[course,sy,sm,sd,ey,em,ed,class_days],
            outputs=[output_box, None, None, None,
                     btn_edit_syllabus, btn_email_syllabus,
                     btn_edit_plan, btn_email_plan]
        )
        btn_edit_syllabus.click(
            enable_edit_syllabus,
            [], [output_box, None, None, None,
                 None, None, None, None]
        )
        btn_email_syllabus.click(
            email_syllabus_callback,
            inputs=[course,instr,email,students,output_box],
            outputs=[output_box, None, None, None,
                     None, None, None, None]
        )
        btn_edit_plan.click(
            enable_edit_plan,
            [], [output_box, None, None, None,
                 None, None, None, None]
        )
        btn_email_plan.click(
            email_plan_callback,
            inputs=[course,instr,email,students,output_box],
            outputs=[output_box, None, None, None,
                     None, None, None, None]
        )
    return demo

# ——— FastAPI + Gradio Mounting ———
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
    return {"status": "ok"}

if __name__ == "__main__":
    build_ui().launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT", 7860)))
