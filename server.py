import difflib, json, os, re, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path

import requests

try:
    import cv2
except ImportError:  # تتبع الوجه اختياري
    cv2 = None
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
jobs = {}
app = FastAPI()
SFX, MUSIC = BASE / "sfx", BASE / "music"
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".m4a"}

# أنماط الكابشنز (الألوان بصيغة BGR الخاصة بـ ASS)
CAP_STYLES = {
    "gold": dict(label="ذهبي كلاسيكي", size=84, fg="&H00FFFFFF", hl="&H0000D7FF", ol="&H00000000", bw=7, n=3, pop=115),
    "red": dict(label="أحمر قوي", size=88, fg="&H00FFFFFF", hl="&H004B4BFF", ol="&H00000000", bw=8, n=3, pop=120),
    "neon": dict(label="سماوي نيون", size=84, fg="&H00FFFFFF", hl="&H00FFE500", ol="&H00502800", bw=6, n=3, pop=115),
    "one": dict(label="كلمة واحدة كبيرة", size=130, fg="&H0000D7FF", hl="&H0000D7FF", ol="&H00000000", bw=9, n=1, pop=125),
    "minimal": dict(label="بسيط أبيض", size=70, fg="&H00FFFFFF", hl="&H00FFFFFF", ol="&H00000000", bw=4, n=4, pop=100),
}


def ensure_sfx():
    """يولّد مؤثرات أساسية تلقائياً؛ وأي ملف تضعه في sfx/ أو music/ يظهر في الصفحة."""
    SFX.mkdir(exist_ok=True)
    MUSIC.mkdir(exist_ok=True)
    gen = {
        "whoosh.wav": "anoisesrc=d=0.6:c=pink:a=0.5,highpass=f=400,lowpass=f=6000,afade=t=in:d=0.25,afade=t=out:st=0.25:d=0.35",
        "pop.wav": "sine=f=900:d=0.12,afade=t=out:d=0.1",
        "ding.wav": "sine=f=1480:d=0.7,afade=t=out:d=0.7",
        "boom.wav": "sine=f=70:d=0.5,afade=t=out:d=0.5,volume=3",
    }
    for name, f in gen.items():
        if not (SFX / name).exists():
            try:
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f, str(SFX / name)], capture_output=True)
            except FileNotFoundError:
                return


ensure_sfx()


def upd(jid, **kw):
    jobs[jid].update(kw)


def sh(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           creationflags=0x4000 if sys.platform == "win32" else 0)
    except FileNotFoundError:
        raise RuntimeError(f"لم يُعثر على البرنامج «{Path(cmd[0]).name}». ثبّته وأضفه إلى PATH ثم أعد فتح الطرفية "
                           "وشغّل السيرفر من جديد (اختبر بالأمر: ffmpeg -version).")
    if r.returncode:
        raise RuntimeError("خطأ في الأداة: " + r.stderr[-500:])


# ---------- 1) التفريغ النصي: AssemblyAI ----------
def transcribe(key, audio):
    h = {"authorization": key}
    with open(audio, "rb") as f:
        up = requests.post("https://api.assemblyai.com/v2/upload", headers=h, data=f).json()
    if "upload_url" not in up:
        raise RuntimeError(f"AssemblyAI: {up}")
    t = requests.post("https://api.assemblyai.com/v2/transcript", headers=h,
                      json={"audio_url": up["upload_url"], "language_detection": True}).json()
    if "id" not in t:
        raise RuntimeError(f"AssemblyAI: {t}")
    while True:
        r = requests.get(f"https://api.assemblyai.com/v2/transcript/{t['id']}", headers=h).json()
        if r["status"] == "completed":
            return r["words"]  # start/end بالميلي ثانية
        if r["status"] == "error":
            raise RuntimeError(f"AssemblyAI: {r.get('error')}")
        time.sleep(3)


# ---------- 2) اختيار المقاطع والهوك: Gemini ----------
GEMINI_DEFAULT = "gemini-3.8-flash"
_MODELS = {}  # آخر موديل نجح لكل مفتاح


def flash_models(key):
    """موديلات flash المتاحة لمفتاحك (generateContent)، الأحدث أولاً."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": key, "pageSize": 200}).json()
    found = []
    for m in r.get("models", []):
        n = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []) or "flash" not in n:
            continue
        if any(x in n for x in ("lite", "image", "tts", "live", "audio", "embed", "robotics")):
            continue
        v = re.search(r"(\d+(?:\.\d+)?)", n)
        found.append((float(v.group(1)) if v else 0.0, n))
    return [n for _, n in sorted(found, reverse=True)]


def gemini_call(key, model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    return requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"responseMimeType": "application/json"}}).json()


def gemini_json(key, prompt, model=""):
    """طلب Gemini مع إعادة المحاولة والانتقال لموديل بديل، ويعيد JSON."""
    model = _MODELS.get(key) or model or GEMINI_DEFAULT
    tried, queue, r = [], [model], {}
    while queue:
        m = queue.pop(0)
        tried.append(m)
        for wait in (0, 4, 10, 20):  # إعادة المحاولة عند الضغط المؤقت
            time.sleep(wait)
            r = gemini_call(key, m, prompt)
            if r.get("error", {}).get("code") not in (429, 500, 503):
                break
        if "candidates" in r:
            model = m
            break
        if r.get("error", {}).get("code") in (404, 429, 500, 503) and len(tried) < 4:
            queue = [x for x in flash_models(key) if x not in tried][:1]  # جرّب موديلاً آخر
        model = m
    try:
        parts = r["candidates"][0]["content"]["parts"]
        clips = json.loads("".join(p.get("text", "") for p in parts))
    except Exception:
        raise RuntimeError(f"Gemini ({model}): {str(r)[:300]}")
    _MODELS[key] = model
    return clips


def ask_gemini(key, lines, n, lo, hi, model=""):
    prompt = (
        f"أنت محرر شورتس محترف. هذا تفريغ فيديو مع الثواني بين الأقواس.\n"
        f"اختر أفضل {n} مقاطع (أو أقل إن لم يوجد ما يستحق)، مدة كل مقطع بين {lo} و{hi} ثانية، "
        f"تبدأ عند بداية جملة وتنتهي عند نهاية فكرة مكتملة، دون تداخل بينها.\n"
        f"لكل مقطع: start و end بالثواني، score من 1 إلى 10، hook (جملة افتتاحية تشد الانتباه "
        f"بحد أقصى 8 كلمات، بنفس لغة الفيديو)، title (عنوان قصير).\n"
        f"أرجع JSON فقط: مصفوفة من الكائنات.\n\n" + "\n".join(lines)
    )
    clips = gemini_json(key, prompt, model)
    return [c for c in clips if float(c["end"]) > float(c["start"])]


def pick_clips(key, words, n, lo, hi, model=""):
    lines = []
    for i in range(0, len(words), 12):
        w = words[i:i + 12]
        t = w[0]["start"] / 1000
        lines.append((t, f"[{t:.1f}] " + " ".join(x["text"] for x in w)))
    total = words[-1]["end"] / 1000
    # فيديو أطول من 30 دقيقة: أجزاء 20 دقيقة متداخلة دقيقة واحدة حتى لا يضيع شيء
    size, step = (1200, 1140) if total > 1800 else (total + 1, total + 1)
    found, t0 = [], 0.0
    while t0 < total:
        part = [l for t, l in lines if t0 <= t < t0 + size]
        if part:
            found += ask_gemini(key, part, n if size > total else min(n, 5), lo, hi, model)
        t0 += step
    found.sort(key=lambda c: -float(c.get("score", 0)))
    chosen = []
    for c in found:  # أفضل المقاطع دون تداخل
        if all(float(c["end"]) <= float(x["start"]) or float(c["start"]) >= float(x["end"]) for x in chosen):
            chosen.append(c)
    return chosen[:n]


# ---------- 3) الوصف والهاشتاجات: Groq ----------
def write_meta(key, clips):
    try:
        items = [{"title": c["title"], "hook": c["hook"]} for c in clips]
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "llama-3.3-70b-versatile",
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content":
                                "اكتب لكل شورت وصفاً من سطرين و5 هاشتاجات بنفس لغة العنوان. "
                                'أرجع JSON بالشكل {"items":[{"description":"","hashtags":[""]}]} '
                                "بنفس ترتيب المدخلات:\n" + json.dumps(items, ensure_ascii=False)}]},
        ).json()
        return json.loads(r["choices"][0]["message"]["content"])["items"]
    except Exception:
        return [{"description": "", "hashtags": []} for _ in clips]


# ---------- 4) الكابشنز المتحركة (ASS) ----------
def ts(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def build_ass(words, start, end, hook, font, st, emph=()):
    head = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,{font},{st['size']},{st['fg']},{st['fg']},{st['ol']},&H80000000,1,0,0,0,100,100,0,0,1,{st['bw']},2,2,60,60,520,1
Style: Hook,{font},100,{st['hl']},{st['hl']},&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,8,70,70,260,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    ev, pops = [], []
    clean = lambda t: t.replace("{", "").replace("}", "")
    dur = end - start
    ev.append(f"Dialogue: 1,{ts(0)},{ts(min(3.0, dur))},Hook,,0,0,0,,"
              r"{\q0\fad(150,250)\fscx60\fscy60\t(0,250,\fscx100\fscy100)}" + clean(hook))
    ws = [w for w in words if w["start"] / 1000 >= start and w["end"] / 1000 <= end]
    rtl_re = re.compile(r"[\u0590-\u08FF]")
    norm = lambda t: re.sub(r"[^\w]", "", t)
    em = {norm(x) for x in emph}
    maxc = max(10, int(900 / (st["size"] * 0.5)))  # أقصى عدد حروف في السطر
    chunks, cur = [], []
    for w in ws:
        if cur and (len(cur) >= st["n"] or sum(len(x["text"]) + 1 for x in cur) + len(w["text"]) > maxc):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        pops.append(chunk[0]["start"] / 1000 - start)
        rtl = sum(bool(rtl_re.search(x["text"])) for x in chunk) * 2 > len(chunk)
        for k, w in enumerate(chunk):
            a = w["start"] / 1000 - start
            b = (chunk[k + 1]["start"] / 1000 - start) if k + 1 < len(chunk) else w["end"] / 1000 - start
            runs = []
            for j, x in enumerate(chunk):
                sc = 100 + (j % 2)  # فرق 1% يجعل كل كلمة جزءاً مستقلاً فلا يخلط libass ترتيبها
                if j == k:
                    tag = r"{\c" + st["hl"] + r"&\fscx" + str(st["pop"]) + r"\fscy" + str(st["pop"]) + "}"
                elif norm(x["text"]) in em:
                    tag = r"{\c&H0000A5FF&\fscx%d\fscy%d}" % (sc, sc)
                else:
                    tag = r"{\c" + st["fg"] + r"&\fscx%d\fscy%d}" % (sc, sc)
                runs.append(tag + clean(x["text"]))
            if rtl:
                runs.reverse()  # libass يضع الأجزاء من اليسار لليمين، فنعكس الترتيب ليُقرأ من اليمين
            ev.append(f"Dialogue: 0,{ts(max(a, 0))},{ts(max(b, a + 0.05))},Cap,,0,0,0,,{' '.join(runs)}")
    return head + "\n".join(ev), pops


# ---------- 5) القص والتحويل 9:16 ----------
def face_track(path, s, e):
    """يعيد None (قص من المنتصف) إن لم يعمل OpenCV بدل إيقاف المعالجة."""
    if cv2 is None or not hasattr(cv2, "CascadeClassifier"):
        print("تحذير: OpenCV غير سليم، سيُستخدم القص من المنتصف.")
        return None
    try:
        return _face_track(path, s, e)
    except Exception as ex:
        print("تحذير: فشل تتبع الوجه:", ex)
        return None


def _face_track(path, s, e):
    """مركز أكبر وجه كل نصف ثانية: (العرض، الارتفاع، [(الزمن، x)])."""
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    W, H = int(cap.get(3)), int(cap.get(4))
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    pts, t, k = [], s, 480 / max(W, 1)
    while t < e:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(cv2.resize(fr, None, fx=k, fy=k), cv2.COLOR_BGR2GRAY)
        f = det.detectMultiScale(g, 1.2, 5, minSize=(30, 30))
        if len(f):
            x, y, w, h = max(f, key=lambda r: r[2] * r[3])
            pts.append((t - s, (x + w / 2) / k))
        t += 0.5
    cap.release()
    return W, H, pts


def crop_expr(W, H, pts, d):
    """تعبير ffmpeg لموضع القص: يتحرك بنعومة ويثبت إن لم يتحرك الوجه كثيراً."""
    cw = int(H * 9 / 16) // 2 * 2
    if not pts or cw >= W:
        return None
    lo, hi = cw / 2, W - cw / 2
    step = 1.0 if d > 40 else 0.5
    cur, track, last = pts[0][1], [], -9.0
    for t, cx in pts:
        if t - last < step - 1e-6:
            continue
        last = t
        if abs(cx - cur) > W * 0.05:
            cur += (cx - cur) * 0.5
        track.append((t, min(max(cur, lo), hi) - cw / 2))
    if track[0][0] > 0.01:
        track.insert(0, (0.0, track[0][1]))
    track.append((d + 1, track[-1][1]))
    terms = []
    for (t0, x0), (t1, x1) in zip(track, track[1:]):
        if t1 - t0 < 0.01:
            continue
        terms.append(f"gte(t,{t0:.2f})*lt(t,{t1:.2f})*({x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{t1 - t0:.2f})")
    return cw, H, "+".join(terms)


FILLERS = {"um", "umm", "uh", "uhh", "er", "erm", "hmm", "hm", "mm", "mmm", "ah", "eh",
           "ممم", "همم", "امم", "اه", "ااه"}


def is_filler(text):
    x = re.sub(r"[^\w]", "", text.lower())
    return not x or x in FILLERS or re.fullmatch(r"[اأإآ]{2,}|م{2,}|ه{2,}", x) is not None


def plan_cuts(words, s, e, gap, pad=0.08):
    """يحدد الأجزاء المحتفظ بها (بالثواني من بداية المقطع) بحذف الصمت الطويل وكلمات الحشو،
    ويعيد الكلمات بتوقيت جديد (ms) يطابق الفيديو بعد القص."""
    dur = e - s
    inr = [w for w in words if w["start"] / 1000 >= s and w["end"] / 1000 <= e]
    if not gap or not inr:
        return [(0.0, dur)], [dict(w, start=w["start"] - int(s * 1000), end=w["end"] - int(s * 1000)) for w in inr]
    groups, cur = [], None
    for w in inr:
        a, b = w["start"] / 1000 - s, w["end"] / 1000 - s
        if is_filler(w["text"]):
            if cur:
                groups.append(cur)
                cur = None
        elif cur and a - cur[1] <= gap:
            cur[1] = b
        else:
            if cur:
                groups.append(cur)
            cur = [a, b]
    if cur:
        groups.append(cur)
    segs, last = [], 0.0
    for a, b in groups:
        a, b = max(a - pad, last, 0.0), min(b + pad, dur)
        if b - a >= 0.2:
            segs.append((a, b))
            last = b
    if not segs or sum(b - a for a, b in segs) < 5:
        return [(0.0, dur)], [dict(w, start=w["start"] - int(s * 1000), end=w["end"] - int(s * 1000)) for w in inr]
    offs, acc = [], 0.0
    for a, b in segs:
        offs.append(acc)
        acc += b - a

    def remap(t):
        for (a, b), o in zip(segs, offs):
            if t <= b:
                return o + max(t - a, 0.0)
        return acc

    mw = [{"text": w["text"], "start": int(remap(w["start"] / 1000 - s) * 1000),
           "end": int(remap(w["end"] / 1000 - s) * 1000)} for w in inr if not is_filler(w["text"])]
    return segs, mw


def render(jdir, i, c, words, o):
    st = CAP_STYLES.get(o["cap"], CAP_STYLES["gold"])
    s, e = float(c["start"]), float(c["end"])
    src_d = e - s
    segs, mw = plan_cuts(words, s, e, o["cut"])
    d = sum(b - a for a, b in segs)  # المدة بعد حذف الصمت
    plan = c.get("plan") or {}
    byfile, evs, off = {}, [], 0.0

    def rt(t):  # من زمن المصدر النسبي إلى الزمن بعد حذف الصمت
        o_ = 0.0
        for a_, b_ in segs:
            if a_ <= t <= b_:
                return o_ + t - a_
            o_ += b_ - a_
        return None

    for e_ in plan.get("events", []):
        nt = rt(float(e_["t"]))
        if nt is not None and nt < d - 0.3:
            evs.append((nt, e_.get("fx", ""), e_.get("sfx", "")))
            if e_.get("sfx") and (SFX / e_["sfx"]).is_file():
                byfile.setdefault(e_["sfx"], []).append(nt)
    ass, pops = build_ass(mw, 0.0, d, c["hook"], o["font"], st, plan.get("emphasis", []))
    (jdir / f"clip{i}.ass").write_text(ass, encoding="utf-8")

    # 1) قص 9:16 (مع تتبع الوجه) على الزمن الأصلي
    tr = face_track(jdir / "src.mp4", s, e) if o["track"] else None
    ce = crop_expr(*tr, src_d) if tr else None
    pre = (f"crop={ce[0]}:{ce[1]}:x='{ce[2]}':y=0" if ce else "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'")
    pre += ",scale=1080:1920"

    # 2) بعد حذف الصمت: تقريب + انتقال + كابشنز على الزمن الجديد
    post = []
    base = {"slow": f"1+0.08*t/{d:.2f}", "pulse": "1+0.04*abs(sin(PI*t/1.4))"}.get(o["zoom"])
    bumps = "".join(f"+gte(t,{t0:.2f})*lt(t,{t0 + 0.7:.2f})*0.09*sin(PI*(t-{t0:.2f})/0.7)"
                    for t0, fx, _ in evs if fx == "punch")
    zoom = ((base or "1") + bumps) if (base or bumps) else None
    fx = {"fade": ("black", 0.3), "flash": ("white", 0.18)}.get(o["trans"])
    if zoom:
        post.append(f"scale=w='trunc(1080*({zoom})/2)*2':h=-2:eval=frame,crop=1080:1920")
    if fx:
        col, fd = fx
        post.append(f"fade=t=in:st=0:d={fd}:color={col},fade=t=out:st={d - fd:.2f}:d={fd}:color={col}")
    for t0, fx, _ in evs:
        if fx == "flash":
            post.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t,{t0:.2f},{t0 + 0.1:.2f})'")
    post.append(f"ass=clip{i}.ass")

    n = len(segs)
    parts = [f"[0:v]{pre}[cv]",
             f"[cv]split={n}" + "".join(f"[c{k}]" for k in range(n)),
             f"[0:a]asplit={n}" + "".join(f"[x{k}]" for k in range(n))]
    for k, (a, b) in enumerate(segs):
        parts.append(f"[c{k}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[sv{k}]")
        parts.append(f"[x{k}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[sa{k}]")
    parts.append("".join(f"[sv{k}][sa{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{','.join(post)}[vo]")
    parts.append("[ac]anull[a0]")

    # 3) الصوت: كلام + مؤثر البداية + نقرات الكابشنز + موسيقى
    cmd = ["ffmpeg", "-y", "-ss", str(s), "-t", str(src_d), "-i", "src.mp4"]
    labels, idx = ["[a0]"], 1

    def add_sfx(name, times, vol):
        nonlocal idx
        times = times[:40]
        cmd.extend(["-i", str(SFX / name)])
        parts.append(f"[{idx}:a]asplit={len(times)}" + "".join(f"[s{idx}_{k}]" for k in range(len(times))))
        for k, tm in enumerate(times):
            ms = int(max(tm, 0) * 1000)
            parts.append(f"[s{idx}_{k}]volume={vol},adelay={ms}|{ms},apad[q{idx}_{k}]")
            labels.append(f"[q{idx}_{k}]")
        idx += 1

    if o["sfx_start"]:
        add_sfx(o["sfx_start"], [0.0], 0.9)
    if o["sfx_chunk"] and pops:
        add_sfx(o["sfx_chunk"], pops, 0.5)
    for name, times in byfile.items():  # مؤثرات اختارها المخرج الذكي
        add_sfx(name, times, 0.75)
    if o["music"]:
        cmd += ["-stream_loop", "-1", "-i", str(MUSIC / o["music"])]
        parts.append(f"[{idx}:a]volume={o['music_vol']}[m]")
        labels.append("[m]")
        idx += 1
    m = len(labels)
    parts.append("".join(labels) + f"amix=inputs={m}:duration=first:dropout_transition=0,volume={m},"
                 f"afade=t=in:d=0.2,afade=t=out:st={d - 0.3:.2f}:d=0.3[ao]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vo]", "-map", "[ao]", "-t", f"{d:.2f}",
            "-threads", str(max(2, (os.cpu_count() or 4) - 1)),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
            "-movflags", "+faststart", f"out/clip{i}.mp4"]
    sh(cmd, jdir)
    # صورة مصغرة خفيفة للعرض في الصفحة بدل تحميل الفيديو كاملاً
    sh(["ffmpeg", "-y", "-ss", "0.8", "-i", f"out/clip{i}.mp4", "-frames:v", "1", "-vf", "scale=360:-2",
        f"out/clip{i}.jpg"], jdir)


def worker(jid, keys, n, lo, hi, o):
    jdir = JOBS_DIR / jid
    try:
        (jdir / "out").mkdir(exist_ok=True)
        if o["url"]:
            upd(jid, step="تنزيل الفيديو (yt-dlp)", progress=4)
            sh([sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4",
                "-o", "src.mp4", o["url"]], jdir)
        upd(jid, step="استخراج الصوت", progress=8)
        sh(["ffmpeg", "-y", "-i", "src.mp4", "-vn", "-ac", "1", "-ar", "16000", "audio.mp3"], jdir)
        upd(jid, step="التفريغ النصي (AssemblyAI)", progress=15)
        words = [{"text": w["text"], "start": w["start"], "end": w["end"]}
                 for w in transcribe(keys["assembly"], jdir / "audio.mp3")]
        (jdir / "words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        upd(jid, step="اختيار أقوى المقاطع (Gemini)", progress=40)
        clips = pick_clips(keys["gemini"], words, n, lo, hi, keys["model"])
        if o.get("auto_fx"):
            upd(jid, step="المخرج الذكي: تأثيرات وأصوات (Gemini)", progress=46)
            try:
                names = sorted(p.name for p in SFX.iterdir() if p.suffix.lower() in AUDIO_EXT)
                for c, pl in zip(clips, direct(keys["gemini"], keys["model"], words, clips, names)):
                    c["plan"] = clean_plan(pl, float(c["end"]) - float(c["start"]), names)
            except Exception as ex:
                print("تحذير: تعذّر المخرج الذكي:", ex)
        upd(jid, step="كتابة الوصف والهاشتاجات (Groq)", progress=50)
        meta = write_meta(keys["groq"], clips) if keys["groq"] else [{} for _ in clips]
        results = []
        for i, c in enumerate(clips):
            upd(jid, step=f"تجهيز الشورت {i + 1} من {len(clips)}", progress=55 + int(40 * i / len(clips)))
            render(jdir, i, c, words, o)
            m = meta[i] if i < len(meta) else {}
            results.append({"i": i, "plan": c.get("plan"), "thumb": f"/jobs/{jid}/out/clip{i}.jpg",
                            "caption": " ".join(w["text"] for w in words if float(c["start"]) <= w["start"] / 1000
                                                and w["end"] / 1000 <= float(c["end"])),
                            "url": f"/jobs/{jid}/out/clip{i}.mp4", "title": c["title"], "hook": c["hook"],
                            "score": c.get("score"), "start": c["start"], "end": c["end"],
                            "description": m.get("description", ""), "hashtags": m.get("hashtags", [])})
        upd(jid, status="done", step="تم", progress=100, clips=results)
        save_project(jid)  # حفظ تلقائي
    except Exception as ex:
        upd(jid, status="error", step="فشل", error=str(ex))


def save_project(jid):
    (JOBS_DIR / jid / "project.json").write_text(json.dumps(jobs[jid], ensure_ascii=False), encoding="utf-8")


def load_job(jid):
    if jid in jobs:
        return jobs[jid]
    f = JOBS_DIR / jid / "project.json"
    if re.fullmatch(r"[0-9a-f]{10}", jid) and f.exists():
        jobs[jid] = json.loads(f.read_text(encoding="utf-8"))
        return jobs[jid]
    return None


def apply_edit(old, new):
    """يطبّق تعديل النص على الكلمات مع الحفاظ على توقيت الكلمات غير المعدّلة."""
    sm = difflib.SequenceMatcher(None, [w["text"] for w in old], new, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += old[i1:i2]
        elif tag in ("replace", "insert"):
            seg = new[j1:j2]
            if i2 > i1:
                t0, t1 = old[i1]["start"], old[i2 - 1]["end"]
            else:
                t0 = old[i1 - 1]["end"] if i1 > 0 else old[0]["start"]
                t1 = t0 + 250 * len(seg)
            step = (t1 - t0) / len(seg)
            out += [{"text": t, "start": int(t0 + k * step), "end": int(t0 + (k + 1) * step)}
                    for k, t in enumerate(seg)]
    return out


class Edit(BaseModel):
    caption: str
    hook: str
    title: str


@app.get("/api/projects")
def projects():
    out = []
    for f in sorted(JOBS_DIR.glob("*/project.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": f.parent.name, "name": j.get("name", ""), "created": j.get("created", ""),
                        "count": len(j.get("clips", []))})
        except Exception:
            pass
    return out


@app.post("/api/jobs/{jid}/clips/{i}/edit")
def edit_clip(jid: str, i: int, e: Edit):
    j = load_job(jid)
    if not j or not 0 <= i < len(j.get("clips", [])):
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    jdir = JOBS_DIR / jid
    wf = jdir / "words.json"
    words = json.loads(wf.read_text(encoding="utf-8"))
    c = j["clips"][i]
    s, en = float(c["start"]), float(c["end"])
    idx = [k for k, w in enumerate(words) if w["start"] / 1000 >= s and w["end"] / 1000 <= en]
    if idx and e.caption.split():
        new = apply_edit([words[k] for k in idx], e.caption.split())
        words[idx[0]:idx[-1] + 1] = new
        wf.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        c["caption"] = " ".join(w["text"] for w in new)
    c["hook"] = e.hook.strip() or c["hook"]
    c["title"] = e.title.strip() or c["title"]
    try:
        render(jdir, i, {"start": s, "end": en, "hook": c["hook"], "title": c["title"], "plan": c.get("plan")},
               words, j["options"])
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)
    c["v"] = int(time.time())  # لتجاوز ذاكرة المتصفح المؤقتة
    save_project(jid)
    return c


def direct(key, model, words, clips, names):
    """Gemini يقرأ كل مقطع ويقرر لحظات التأثيرات البصرية والصوتية والكلمات المفتاحية."""
    items = []
    for i, c in enumerate(clips):
        s0, e0 = float(c["start"]), float(c["end"])
        ws = [w for w in words if w["start"] / 1000 >= s0 and w["end"] / 1000 <= e0]
        lines = [f"[{ws[k]['start'] / 1000 - s0:.1f}] " + " ".join(x["text"] for x in ws[k:k + 6])
                 for k in range(0, len(ws), 6)]
        items.append(f"### مقطع {i} (المدة {e0 - s0:.0f} ثانية)\n" + "\n".join(lines))
    prompt = (
        "أنت مخرج مونتاج شورتس محترف. لكل مقطع أدناه (التوقيت بالثواني من بداية المقطع) اقترح ما يجعله جذاباً:\n"
        "- events: لحظات قوية (جملة هوك، مفاجأة، رقم صادم، نقطة تحول، ضحك، كلمة مهمة). لكل حدث: "
        "t بالثواني، وfx واحد من: punch (تقريب سريع) أو flash (وميض) أو none، "
        f"وsfx اسم ملف من هذه القائمة فقط أو سلسلة فارغة: {', '.join(names)}.\n"
        "- لا تزد عن حدث كل 3 ثوانٍ تقريباً وبحد أقصى 8 أحداث لكل مقطع، ولا تفرط في المؤثرات.\n"
        "- emphasis: حتى 6 كلمات مفتاحية (كما وردت في النص تماماً) تُلوَّن في الكابشنز.\n"
        'أرجع JSON فقط: مصفوفة بعدد المقاطع وبنفس ترتيبها، كل عنصر على الشكل '
        '{"events":[{"t":0.5,"fx":"punch","sfx":"whoosh.wav"}],"emphasis":["كلمة"]}.\n\n' + "\n\n".join(items)
    )
    plans = gemini_json(key, prompt, model)
    return plans if isinstance(plans, list) else []


def clean_plan(p, dur, names):
    p = p if isinstance(p, dict) else {}
    evs = []
    for e in (p.get("events") if isinstance(p.get("events"), list) else []):
        try:
            t = float(e.get("t"))
        except Exception:
            continue
        if 0 <= t < dur - 0.5:
            evs.append({"t": round(t, 2), "fx": e.get("fx") if e.get("fx") in ("punch", "flash") else "",
                        "sfx": e.get("sfx") if e.get("sfx") in names else ""})
    evs.sort(key=lambda x: x["t"])
    out, last = [], -9.0
    for e in evs:
        if (e["fx"] or e["sfx"]) and e["t"] - last >= 1.5:
            out.append(e)
            last = e["t"]
    emph = [str(x) for x in p.get("emphasis")][:6] if isinstance(p.get("emphasis"), list) else []
    return {"events": out[:8], "emphasis": emph}


def pick(folder, name):
    n = Path(name).name if name else ""
    return n if n and (folder / n).is_file() else ""


@app.get("/api/assets")
def assets():
    ls = lambda d: sorted(p.name for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT)
    return {"sfx": ls(SFX), "music": ls(MUSIC), "caps": {k: v["label"] for k, v in CAP_STYLES.items()}}


@app.post("/api/jobs")
def create_job(video: UploadFile = File(None), assembly_key: str = Form(...), gemini_key: str = Form(...),
               groq_key: str = Form(""), clips: int = Form(5), min_s: int = Form(25), max_s: int = Form(60),
               font: str = Form("Arial"), cap: str = Form("gold"), zoom: str = Form("slow"),
               trans: str = Form("fade"), sfx_start: str = Form(""), sfx_chunk: str = Form(""),
               music: str = Form(""), music_vol: float = Form(0.12), url: str = Form(""), track: str = Form("1"), cut: float = Form(0.45), gemini_model: str = Form(""), auto_fx: str = Form("1")):
    if video is None and not url.startswith("http"):
        return JSONResponse({"error": "no video"}, status_code=400)
    jid = uuid.uuid4().hex[:10]
    jdir = JOBS_DIR / jid
    jdir.mkdir()
    if video is not None:
        with open(jdir / "src.mp4", "wb") as f:
            shutil.copyfileobj(video.file, f)
    jobs[jid] = {"status": "running", "step": "بدء", "progress": 2, "clips": [],
                 "name": (video.filename if video is not None else url)[:80], "created": time.strftime("%Y-%m-%d %H:%M")}
    keys = {"assembly": assembly_key, "gemini": gemini_key, "groq": groq_key,
            "model": re.sub(r"[^\w.\-]", "", gemini_model)}
    o = {"font": font, "cap": cap, "zoom": zoom, "trans": trans, "sfx_start": pick(SFX, sfx_start),
         "sfx_chunk": pick(SFX, sfx_chunk), "music": pick(MUSIC, music),
         "music_vol": round(min(max(music_vol, 0.0), 1.0), 2),
         "url": url if url.startswith("http") else "", "track": track == "1", "cut": round(min(max(cut, 0.0), 3.0), 2), "auto_fx": auto_fx == "1"}
    jobs[jid]["options"] = o
    threading.Thread(target=worker, args=(jid, keys, clips, min_s, max_s, o), daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return load_job(jid) or JSONResponse({"error": "غير موجود"}, status_code=404)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/jobs", StaticFiles(directory=JOBS_DIR), name="jobs")