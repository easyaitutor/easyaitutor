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
 days_map = {"Monday":0,"Tuesday":1,"Wednesday":2,
            "Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}

# PDF loader & Section Splitter
try:
    import fitz
    def split_sections(pdf_file):
        doc = fitz.open(pdf_file.name) if hasattr(pdf_file,"name") else fitz.open(stream=pdf_file.read(),filetype="pdf")
        pages = [p.get_text() for p in doc]
        doc.close()
        headings=[]
        for i,text in enumerate(pages):
            for m in re.finditer(r"(?m)^(?:CHAPTER|Cap[ií]tulo)\s+.*",text,re.IGNORECASE):
                headings.append({'page':i+1,'start':(i,m.start()),'title':m.group().strip()})
        headings.sort(key=lambda h:(h['page'],h['start'][1]))
        secs=[]
        for idx,h in enumerate(headings):
            sp,so=h['start']; ep,eo=(headings[idx+1]['start'] if idx+1<len(headings) else (len(pages)-1,len(pages[-1])))
            txt=""
            for p in range(sp,ep+1):
                segment=pages[p]
                if p==sp: segment=segment[so:]
                elif p==ep: segment=segment[:eo]
                txt+=segment+"\n"
            secs.append({'title':h['title'],'content':txt.strip(),'page':h['page']})
        return secs
except ImportError:
    from PyPDF2 import PdfReader
    def split_sections(pdf_file):
        reader=PdfReader(pdf_file.name) if hasattr(pdf_file,"name") else PdfReader(io.BytesIO(pdf_file.read()))
        text="\n".join(p.extract_text() or '' for p in reader.pages)
        chunks=re.split(r'(?<=[.?!])\s+',text)
        return [{'title':f'Lesson {i//5+1}','content':' '.join(chunks[i:i+5]).strip(),'page':None} for i in range(0,len(chunks),5)]

# Syllabus
def count_classes(sd,ed,wdays):
    cnt=0;cur=sd
    while cur<=ed:
        if cur.weekday() in wdays: cnt+=1
        cur+=timedelta(days=1)
    return cnt

def generate_syllabus(cfg):
    sd=datetime.strptime(cfg['start_date'],'%Y-%m-%d').date()
    ed=datetime.strptime(cfg['end_date'],'%Y-%m-%d').date()
    mr=f"{sd.strftime('%B')}–{ed.strftime('%B')}"
    total=count_classes(sd,ed,[days_map[d] for d in cfg['class_days']])
    header=[
        f"Course Name: {cfg['course_name']}",
        f"Professor:   {cfg['instructor']['name']}",
        f"Email:       {cfg['instructor']['email']}",
        f"Duration:    {mr} ({total} classes)",
        '_'*60
    ]
    objs=[f" • {o}" for o in cfg['learning_objectives']]
    body=["COURSE DESCRIPTION:",cfg['course_description'],"","OBJECTIVES:"]+objs+[
        "","GRADING & ASSESSMENTS:",
        " • Each class includes a quiz.",
        " • If score < 60%, student may retake.",
        " • Final grade = average.",
        "","SCHEDULE OVERVIEW:",
        f" • {mr}, every {', '.join(cfg['class_days'])}",
        "","OFFICE HOURS & SUPPORT:",
        " • Office Hours: Tue 3–5PM; Thu 10–11AM (Zoom)",
        " • Email response <24h weekdays"
    ]
    return "\n".join(header+['']+body)

# Lesson Plan
def generate_plan_by_week(cfg):
    sd=datetime.strptime(cfg['start_date'],'%Y-%m-%d').date()
    ed=datetime.strptime(cfg['end_date'],'%Y-%m-%d').date()
    wdays={days_map[d] for d in cfg['class_days']}
    dates=[];cur=sd
    while cur<=ed:
        if cur.weekday() in wdays: dates.append(cur)
        cur+=timedelta(days=1)
    sums=[]
    for sec in cfg['sections']:
        r=openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"Summarize this section in one clear sentence."},
                {"role":"user","content":sec['content']}
            ],temperature=0.7,max_tokens=60
        )
        sums.append(r.choices[0].message.content.strip())
    weeks={}
    for i,d in enumerate(dates):
        wno=d.isocalendar()[1]
        weeks.setdefault(wno,[]).append((i+1,d,sums[i] if i<len(sums) else 'TBA',cfg['sections'][i]['page'] if i<len(cfg['sections']) else None))
    out=[]
    for wno in sorted(weeks):
        out.append(f"## Week {wno}\n")
        for num,d,s,p in weeks[wno]:
            ds=d.strftime('%B %d, %Y');pi=f" (p. {p})" if p else ''
            out.append(f"**Lesson {num} ({ds}){pi}:** {s}")
        out.append('')
    return "\n".join(out)

# Callbacks
def save_setup(course_name,instr_name,instr_email,devices,pdf_file,sy,sm,sd,ey,em,ed,class_days,students):
    try:
        secs=split_sections(pdf_file)
        full="\n\n".join(f"{s['title']}\n{s['content']}" for s in secs)
        r1=openai.chat.completions.create(model="gpt-3.5-turbo",messages=[{"role":"system","content":"Generate concise course description."},{"role":"user","content":full}])
        desc=r1.choices[0].message.content.strip()
        r2=openai.chat.completions.create(model="gpt-3.5-turbo",messages=[{"role":"system","content":"Generate 5–12 learning objectives."},{"role":"user","content":full}])
        objs=[ln.strip(' -•') for ln in r2.choices[0].message.content.splitlines() if ln.strip()]
        cfg={"course_name":course_name,"instructor":{"name":instr_name,"email":instr_email},"class_days":class_days,
             "start_date":f"{sy}-{sm}-{sd}","end_date":f"{ey}-{em}-{ed}","sections":secs,
             "course_description":desc,"learning_objectives":objs}
        p=CONFIG_DIR/f"{course_name.replace(' ','_').lower()}_config.json";p.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
        syl=generate_syllabus(cfg)
        return (gr.update(value=syl,interactive=False),gr.update(visible=False),gr.update(visible=True),gr.update(visible=True),gr.update(visible=False),gr.update(visible=False),gr.update(visible=False),gr.update(visible=False))
    except Exception:
        err=f"⚠️ Error:\n{traceback.format_exc()}"
        return (gr.update(value=err,interactive=False),gr.update(visible=True),*(gr.update(visible=False),)*6)

def show_syllabus_callback(course_name):
    try:
        p=CONFIG_DIR/f"{course_name.replace(' ','_').lower()}_config.json";cfg=json.loads(p.read_text(encoding="utf-8"))
        syl=generate_syllabus(cfg)
        return (gr.update(value=syl,interactive=False),gr.update(visible=False),gr.update(visible=False),gr.update(visible=True),gr.update(visible=True),gr.update(visible=False),gr.update(visible=False),gr.update(visible=False))
    except Exception:
        return (gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}",interactive=False),*(None,)*7)

def enable_edit_syllabus():
    return (gr.update(interactive=True),*(None,)*7)

def generate_plan_callback(course_name,sy,sm,sd,ey,em,ed,class_days):
    try:
        p=CONFIG_DIR/f"{course_name.replace(' ','_').lower()}_config.json";cfg=json.loads(p.read_text(encoding="utf-8"))
        plan=generate_plan_by_week(cfg)
        return (gr.update(value=plan,interactive=False),gr.update(visible=False),gr.update(visible=True),gr.update(visible=False),gr.update(visible=False),gr.update(visible=True),gr.update(visible=True),gr.update(visible=False))
    except Exception:
        return (gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}",interactive=False),*(None,)*7)

def enable_edit_plan():
    return (gr.update(interactive=True),*(None,)*7)

def email_syllabus_callback(course_name,instr_name,instr_email,students,output):
    try:
        buf,fn=download_docx(output,f"{course_name}_syllabus.docx");data=buf.read();
        recs=[(instr_name,instr_email)]+[(n.strip(),e.strip()) for ln in students.splitlines() if ',' in ln for n,e in [ln.split(',',1)]]
        for n,e in recs:
            msg=EmailMessage();msg["Subject"]=f"Course Syllabus: {course_name}";msg["From"]=SMTP_USER;msg["To"]=e
            msg.set_content(f"Hi {n},\n\nAttached is the syllabus for {course_name}.\n\nBest,\nAI Tutor Bot")
            msg.add_attachment(data,maintype="application",subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",filename=fn)
            with smtplib.SMTP(SMTP_SERVER,SMTP_PORT) as s:s.starttls();s.login(SMTP_USER,SMTP_PASS);s.send_message(msg)
        return (gr.update(value="✅ Syllabus emailed!",interactive=False),*(None,)*7)
    except Exception:
        return (gr.update(value=f"⚠️ Error:\n{traceback.format_exc()}",interactive=False),*(None,)*7)

def email_plan_callback(course_name,instr_name,instr_email,students,output):
    try:
        buf,fn=download_docx(output,f"{course_name}_lesson_plan
