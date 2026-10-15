"""Employee Attendance & Analytics API."""
import os, re, math
import json
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from calendar import monthrange
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from datetime import UTC
from bson import json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
NONPRESENCE = {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_1")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_1_date_1")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_code_date")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")

def err422(msg): raise HTTPException(status_code=422, detail=msg)
def parse_date(s: str) -> date:
    try:
        if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): raise ValueError()
        return date.fromisoformat(s)
    except Exception: err422("Date must be YYYY-MM-DD")
def parse_month(s: str) -> tuple[date,date]:
    if not isinstance(s,str) or not re.fullmatch(r"\d{4}-\d{2}",s): err422("Month must be YYYY-MM")
    try: y,m=map(int,s.split("-")); start=date(y,m,1); end=date(y,m,monthrange(y,m)[1]); return start,end
    except Exception: err422("Month must be YYYY-MM")
def parse_hhmm(s: str) -> tuple[int,int]:
    if not isinstance(s,str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",s): err422("Time must be HH:MM")
    h,m=map(int,s.split(":")); return h,m
def round2(v): return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def round4(v): return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
def now_ist(): return datetime.now(IST).replace(microsecond=0)
def normalize_dt(dt):
    if dt.tzinfo is None: dt=dt.replace(tzinfo=IST)
    return dt.astimezone(UTC).replace(microsecond=0)
def parse_ms(value):
    if isinstance(value,bool) or not isinstance(value,int) or not 100_000_000_000 <= value <= 4_102_444_800_000: err422("Timestamp must be epoch milliseconds in the allowed range")
    return datetime.fromtimestamp(value/1000, tz=UTC).replace(microsecond=0)
def get_timestamp(value): return now_ist().astimezone(UTC) if value is None else parse_ms(value)
def ist_dt(dt):
    if dt is None: return None
    if dt.tzinfo is None: return dt.replace(tzinfo=UTC).astimezone(IST)
    return dt.astimezone(IST)
def date_for_punch(ts, emp):
    local=ist_dt(ts); start=parse_hhmm(emp.get("shift_start","09:30")); end=parse_hhmm(emp.get("shift_end","18:30"))
    if end <= start and (local.hour,local.minute) < end: return (local.date()-timedelta(days=1)).isoformat()
    return local.date().isoformat()
def shift_end_dt(day, shift_end, shift_start):
    eh,em=parse_hhmm(shift_end); sh,sm=parse_hhmm(shift_start)
    d=date.fromisoformat(day)
    if (eh,em) <= (sh,sm): d += timedelta(days=1)
    return datetime.combine(d,time(eh,em),IST).astimezone(UTC)
def derived(punch_in,punch_out,emp,day):
    late=0; overtime=0; hours=None; half=False
    
    if punch_in:
        # Convert punch-in time to UTC if needed
        if punch_in.tzinfo is None:
            punch_in = punch_in.replace(tzinfo=IST)
        else:
            punch_in = punch_in.astimezone(UTC)

        # Convert to Indian Standard Time
        local = punch_in.astimezone(IST)

        # Get shift start time and attendance date
        h, m = parse_hhmm(emp.get("shift_start", "09:30"))
        attendance_day = date.fromisoformat(day)
        start = datetime.combine(attendance_day, time(h, m), IST)

        # Calculate minutes since shift start
        elapsed_seconds = (local - start).total_seconds()
        late_elapsed = int(elapsed_seconds // 60)

        # Allow 10 minutes of grace
        if elapsed_seconds > 600:
            late = max(0, late_elapsed)


    
    
    if punch_in and punch_out:
        # Convert both timestamps to UTC
        if punch_in.tzinfo is None:
            punch_in = punch_in.replace(tzinfo=IST)
        else:
            punch_in = punch_in.astimezone(UTC)

        if punch_out.tzinfo is None:
            punch_out = punch_out.replace(tzinfo=IST)
        else:
            punch_out = punch_out.astimezone(UTC)

        # Calculate work duration
        secs = (punch_out - punch_in).total_seconds()
        hours = round2(secs / 3600)


        end = shift_end_dt(
            day,
            emp.get("shift_end", "18:30"),
            emp.get("shift_start", "09:30")
        )
        if end.tzinfo is None:
           
           end = end.replace(tzinfo=IST)
           end = end.astimezone(UTC)

        over = int((punch_out - end).total_seconds() // 60)
        overtime = over if over >= 30 else 0
        half = hours < 4.50

    return {
        "work_hours": hours,
        "late_minutes": int(late),
        "overtime_minutes": int(overtime),
        "half_day": bool(half)
    }

def clean(doc):
    if doc is None:return None
    doc=dict(doc); oid=doc.pop("_id",None)
    if oid is not None: doc["id"]=str(oid)
    doc.setdefault("history",[]); doc.setdefault("half_day",False)
    return doc
def employee_or_404(code):
    e=db.employees.find_one({"emp_code":code})
    if not e: raise HTTPException(404,"Employee not found")
    return e
def valid_page(page,page_size):
    if page<1 or page_size<1 or page_size>100: err422("page must be >=1 and page_size must be between 1 and 100")
class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str; name:str=Field(min_length=1,max_length=100); email:str=Field(min_length=3,max_length=120); department:str=Field(min_length=1,max_length=50)
    shift_start:str="09:30"; shift_end:str="18:30"; joined_on:str
    @field_validator("emp_code")
    @classmethod
    def code_valid(cls,v):
        if not re.fullmatch(r"EMP\d{4,6}",v): raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return v
    @field_validator("email")
    @classmethod
    def email_valid(cls,v):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",v): raise ValueError("Invalid email")
        return v
    @field_validator("shift_start","shift_end")
    @classmethod
    def shift_valid(cls,v):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v): raise ValueError("Shift time must be HH:MM")
        return v
    @field_validator("joined_on")
    @classmethod
    def joined_valid(cls,v):
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",v): raise ValueError()
            date.fromisoformat(v)
        except Exception: raise ValueError("joined_on must be YYYY-MM-DD")
        return v
    @field_validator("shift_end")
    @classmethod
    def shift_different(cls,v,info):
        start=info.data.get("shift_start")
        if start==v: raise ValueError("shift_start and shift_end must differ")
        return v
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str; punched_at:Optional[int]=None; status:str="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp_valid(cls,v):
        if v is not None: parse_ms(v)
        return v
    @field_validator("status")
    @classmethod
    def status_valid(cls,v):
        if v not in PRESENCE: raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str; punched_at:Optional[int]=None
    @field_validator("punched_at")
    @classmethod
    def timestamp_valid(cls,v):
        if v is not None: parse_ms(v)
        return v
class PatchIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    reason:str=Field(min_length=5,max_length=200); regularized_by:str=Field(min_length=1,max_length=50)
    status:Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in:Optional[int]=None; punch_out:Optional[int]=None
    @field_validator("punch_in","punch_out")
    @classmethod
    def timestamp_valid(cls,v):
        if v is not None: parse_ms(v)
        return v

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"MongoDB unavailable")
@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_ist().replace(tzinfo=None)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return clean(doc)
@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=1,page_size:int=20):
    valid_page(page,page_size); q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q); items=[clean(x) for x in db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}
@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=employee_or_404(body.emp_code); ts=get_timestamp(body.punched_at); day= date_for_punch(ts,emp)
    if db.attendance_logs.find_one({"emp_code":body.emp_code,"date":day}): raise HTTPException(409,"already punched in for this date")
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":derived(ts,None,emp,day)["late_minutes"],"overtime_minutes":0,"half_day":False,"history":[]}
    try: res=db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    doc["id"]=str(res.inserted_id); return clean(doc)
@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    employee=employee_or_404(body.emp_code); ts=get_timestamp(body.punched_at)
    record=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not record: raise HTTPException(404,"No punch-in record found")
    if record.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    punch_in_ts = record["punch_in"]

    if punch_in_ts.tzinfo is None:
       punch_in_ts = punch_in_ts.replace(tzinfo=UTC)

    if ts <= punch_in_ts or ts > punch_in_ts + timedelta(hours=24):
       err422("punch_out must be after punch_in and within 24 hours")
    vals=derived(record["punch_in"],ts,employee,record["date"])
    result=db.attendance_logs.update_one({"_id":record["_id"],"punch_out":None},{"$set":{"punch_out":ts,**vals}})
    if result.modified_count!=1: raise HTTPException(409,"already punched out")
    record.update({"punch_out":ts,**vals}); return clean(record)
@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=1,page_size:int=20):
    valid_page(page,page_size); q={}
    if emp_code:q["emp_code"]=emp_code
    if status:q["status"]=status
    if date_from:
        parse_date(date_from); q.setdefault("date",{})["$gte"]=date_from
    if date_to:
        parse_date(date_to); q.setdefault("date",{})["$lte"]=date_to
    if date_from and date_to and date_from>date_to: err422("date_from must be <= date_to")
    total=db.attendance_logs.count_documents(q); docs=[clean(x) for x in db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)]
    return {"items":docs,"total":total,"page":page,"page_size":page_size}
@app.patch("/attendance/{emp_code}/{day}")
def patch_attendance(emp_code:str,day:str,body:PatchIn):
    parse_date(day); emp=employee_or_404(emp_code); old=db.attendance_logs.find_one({"emp_code":emp_code,"date":day})
    if not old: raise HTTPException(404,"Attendance record not found")
    status=body.status if body.status is not None else old.get("status","PRESENT")
    pin=old.get("punch_in"); pout=old.get("punch_out")
    if "punch_in" in body.model_fields_set: pin=parse_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in body.model_fields_set: pout=parse_ms(body.punch_out) if body.punch_out is not None else None
    if status in NONPRESENCE:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None): err422("ABSENT/LEAVE cannot have punch times")
        pin=pout=None
    elif pin is None: err422("Presence status requires punch_in")
    if pin is not None and date_for_punch(pin,emp)!=day: err422("punch_in does not map to the attendance date")
    if pout is not None:
        if pin is None or pout<=pin or pout>pin+timedelta(hours=24): err422("punch_out must be after punch_in and within 24 hours")
    
    vals=derived(pin,pout,emp,day) if status in PRESENCE else {"work_hours":None,"late_minutes":0,"overtime_minutes":0,"half_day":False}
    vals["late_minutes"] = derived(pin, None, emp, day)["late_minutes"] if status in PRESENCE and pin is not None else vals["late_minutes"]
    print("DEBUG old late:", old.get("late_minutes"))
    print("DEBUG new late:", vals.get("late_minutes"))
    print("DEBUG punch-in:", pin)
    print("DEBUG shift start:", emp.get("shift_start"))
    changes={}
    for key,val in [("status",status),("punch_in",pin),("punch_out",pout),*vals.items()]:
        before=old.get(key,False if key=="half_day" else None)
        if before!=val: changes[key]=val
    if not changes:
        err422("No changes supplied")
    entry={"at":now_ist().replace(tzinfo=None),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Atomic push + update avoids losing concurrent history entries.
    update={"$set":changes,"$push":{"history":entry}}
    result=db.attendance_logs.update_one({"_id":old["_id"]},update)
    if result.modified_count!=1: raise HTTPException(409,"Concurrent edit; retry")
    fresh=db.attendance_logs.find_one({"_id":old["_id"]}); return clean(fresh)

def working_days(start,end): return sum(1 for n in range((end-start).days+1) if (start+timedelta(days=n)).weekday()<5)
def presence(status): return status in PRESENCE
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r"^\d{4}-\d{2}$")):
    emp=employee_or_404(emp_code); start,end=parse_month(month); joined=parse_date(emp["joined_on"]); effective=max(start,joined)
    wd=working_days(effective,end) if effective<=end else 0
    docs=list(db.attendance_logs.find({"emp_code":emp_code,"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}))
    present=sum(1 for x in docs if presence(x.get("status")) and parse_date(x["date"]).weekday()<5)
    leaves=sum(1 for x in docs if x.get("status")=="LEAVE")
    late=[x for x in docs if int(x.get("late_minutes",0) or 0)>0]
    pct=round2(present/wd*100) if wd else None
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":present,"leave_days":leaves,"late_count":len(late),"total_late_minutes":sum(int(x.get("late_minutes",0) or 0) for x in docs),"total_overtime_minutes":sum(int(x.get("overtime_minutes",0) or 0) for x in docs),"attendance_pct":pct}
@app.get("/analytics/departments/summary")
def department_summary(month:str=Query(...,pattern=r"^\d{4}-\d{2}$"),department:Optional[str]=None):
    start,end=parse_month(month); eq={"joined_on":{"$lte":end.isoformat()}}
    if department is not None:eq["department"]=department
    emps=list(db.employees.find(eq,{"_id":0})); bydept={}
    for e in emps: bydept.setdefault(e["department"],[]).append(e)
    output=[]
    for dep,group in sorted(bydept.items()):
        codes=[e["emp_code"] for e in group]; logs=list(db.attendance_logs.find({"emp_code":{"$in":codes},"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}})) if codes else []
        pres=[x for x in logs if presence(x.get("status"))]; wh=[float(x["work_hours"]) for x in pres if x.get("work_hours") is not None]
        output.append({"department":dep,"headcount":len(group),"present_days":sum(1 for x in pres if parse_date(x["date"]).weekday()<5),"avg_work_hours":round2(sum(wh)/len(wh)) if wh else None,"late_count":sum(1 for x in logs if int(x.get("late_minutes",0) or 0)>0),"total_late_minutes":sum(int(x.get("late_minutes",0) or 0) for x in logs),"leave_count":sum(1 for x in logs if x.get("status")=="LEAVE"),"on_duty_count":sum(1 for x in logs if x.get("status")=="ON_DUTY")})
    return {"month":month,"items":output}
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str=Query(...,pattern=r"^\d{4}-\d{2}$"),limit:int=Query(10,ge=1,le=100),department:Optional[str]=None):
    start,end=parse_month(month); match={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"late_minutes":{"$gt":0}}
    pipeline=[{"$match":match},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},{"$sort":{"total_late_minutes":-1,"_id":1}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None:pipeline.append({"$match":{"employee.department":department}})
    pipeline += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$project":{"_id":0,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1,"rank":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipeline))}
@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_date:str=Query(...,alias="from"),to_date:str=Query(...,alias="to")):
    start=parse_date(from_date); end=parse_date(to_date)
    if end<start:err422("to must be >= from")
    if (end-start).days+1>92:err422("Date range cannot exceed 92 days")
    if not db.employees.find_one({"department":department}):raise HTTPException(404,"Department not found")
    # MongoDB performs daily grouping, date gap filling and seven-row window average.
    pipeline=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"},{"$match":{"emp.department":department}},{"$set":{"day_date":{"$dateFromString":{"dateString":"$date"}},"is_present":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":["$half_day",0.5,1]},0]},"is_late":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}},{"$group":{"_id":"$day_date","present_count":{"$sum":"$is_present"},"late_count":{"$sum":"$is_late"}}},{"$densify":{"field":"_id","range":{"step":1,"unit":"day","bounds":[datetime.combine(start,time.min),datetime.combine(end,time.min)]}}},{"$set":{"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]},"is_working_day":{"$lte":[{"$dayOfWeek":"$_id"},6]}}},{"$lookup":{"from":"employees","let":{"d":{"$dateToString":{"format":"%Y-%m-%d","date":"$_id"}},"dep":department},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department","$$dep"]},{"$lte":["$joined_on","$$d"]}]}}},{"$count":"n"}],"as":"hc"}},{"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]}}},{"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},{"$setWindowFields":{"sortBy":{"_id":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},{"$project":{"_id":0,"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$_id"}},"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}},{"$sort":{"date":1}}]
    try: rows=list(db.attendance_logs.aggregate(pipeline))
    except Exception as ex: raise HTTPException(500,"Trend aggregation failed; verify MongoDB 6.0+")
    # Ensure empty days are still returned if the collection has no rows at all in the range.
    got={r["date"]:r for r in rows}; output=[]
    for i in range((end-start).days+1):
        d=start+timedelta(days=i); key=d.isoformat()
        if key in got: output.append(got[key]); continue
        head=db.employees.count_documents({"department":department,"joined_on":{"$lte":key}}); work=d.weekday()<5
        rate=0.0 if work and head else None
        prev=[x["attendance_rate"] for x in output[-6:] if x.get("attendance_rate") is not None]
        if rate is not None: prev.append(rate)
        output.append({"date":key,"is_working_day":work,"headcount":head,"present_count":0,"late_count":0,"attendance_rate":rate,"moving_avg_7d":round4(sum(prev)/len(prev)) if prev else None})
    return {"department":department,"from":from_date,"to":to_date,"items":output}
@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,month:Optional[str]=None,emp_code:Optional[str]=None,department:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,page:int=1,page_size:int=20,limit:int=10):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed:err422("Unknown endpoint")
    valid_page(page,page_size)
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if department:
            emps=[x["emp_code"] for x in db.employees.find({"department":department},{"emp_code":1,"_id":0})]; q["emp_code"]={"$in":emps}
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=parse_date(date_from).isoformat()
            if date_to:q["date"]["$lte"]=parse_date(date_to).isoformat()
        cursor=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
        return json.loads(json_util.dumps(cursor.explain()))
    if endpoint != "department_trend" and not month:
        err422("month=YYYY-MM is required for this endpoint")
    if endpoint != "department_trend":
        start, end = parse_month(month)
    if endpoint=="employee_monthly":
        if not emp_code:err422("emp_code is required")
        pipeline=[{"$match":{"emp_code":emp_code,"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},{"$sort":{"date":1}}]
        return json.loads(json_util.dumps(
        db.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        },
        verbosity="executionStats"
    )
))
    if endpoint=="department_summary":
        pipeline=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},{"$sort":{"date":1}}]
        return json.loads(json_util.dumps(
        db.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        },
        verbosity="executionStats"
    )
))
    if endpoint=="late_leaderboard":
        match={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"late_minutes":{"$gt":0}}
        if emp_code:match["emp_code"]=emp_code
        pipeline=[{"$match":match},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"}}},{"$sort":{"total_late_minutes":-1,"_id":1}},{"$limit":limit}]
        return json.loads(json_util.dumps(
        db.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        },
        verbosity="executionStats"
    )
))
    # Trend's principal indexed date-range query.
    q={"date":{"$gte":(parse_date(date_from).isoformat() if date_from else start.isoformat()),"$lte":(parse_date(date_to).isoformat() if date_to else end.isoformat())}}
    return json.loads(json_util.dumps(
    db.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": [{"$match": q}, {"$sort": {"date": 1}}],
            "cursor": {}
        },
        verbosity="executionStats"
    )
))
