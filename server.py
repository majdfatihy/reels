import base64, difflib, json, os, re, shutil, subprocess, sys, threading, time, uuid
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
SFX, MUSIC, ICONS = BASE / "sfx", BASE / "music", BASE / "icons"
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".m4a"}

# أنماط الكابشنز (الألوان بصيغة BGR الخاصة بـ ASS)
def bgr(hx):
    return "&H00" + (hx[5:7] + hx[3:5] + hx[1:3]).upper()


def _S(label, fg, hl, ol, bw, n=3, size=84, pop=115, mode="stroke", bx=None):
    return dict(label=label, fg=bgr(fg), hl=bgr(hl), ol=bgr(ol), bw=bw, n=n, size=size, pop=pop, mv=520,
                hx=[fg, hl, ol], mode=mode, bx=bgr(bx or ol), bxh=bx or ol)


CAP_STYLES = {
    "gold": _S("ذهبي كلاسيكي", "#FFFFFF", "#FFD700", "#000000", 7),
    "red": _S("أحمر قوي", "#FFFFFF", "#FF4B4B", "#000000", 8, size=88, pop=120),
    "neon": _S("سماوي نيون", "#FFFFFF", "#00E5FF", "#002850", 6),
    "one": _S("كلمة واحدة كبيرة", "#FFD700", "#FFD700", "#000000", 9, n=1, size=130, pop=125),
    "minimal": _S("بسيط أبيض", "#FFFFFF", "#FFFFFF", "#000000", 4, n=4, size=70, pop=100),
    "blackbox": _S("صندوق أسود للكلمة", "#FFFFFF", "#FFFFFF", "#000000", 7, mode="wordbox", bx="#000000"),
    "whitebox": _S("صندوق أبيض للكلمة", "#FFFFFF", "#000000", "#000000", 7, mode="wordbox", bx="#FFFFFF"),
    "violetbox": _S("صندوق بنفسجي للكلمة", "#FFFFFF", "#FFFFFF", "#000000", 7, mode="wordbox", bx="#7C00FF"),
    "yellowbox": _S("صندوق أصفر للكلمة", "#FFFFFF", "#000000", "#000000", 7, mode="wordbox", bx="#FFD700"),
    "allblack": _S("خلفية سوداء للنص", "#FFFFFF", "#FFFFFF", "#000000", 14, mode="allbox"),
    "allwhite": _S("خلفية بيضاء للنص", "#000000", "#000000", "#FFFFFF", 14, mode="allbox"),
    "allviolet": _S("خلفية بنفسجية للنص", "#FFFFFF", "#FFFFFF", "#7C00FF", 14, mode="allbox"),
    "allyellow": _S("خلفية صفراء للنص", "#000000", "#000000", "#FFD700", 14, mode="allbox"),
    "greenstroke": _S("أخضر بحدود سوداء", "#00FF40", "#FFFFFF", "#000000", 9),
    "bluestroke": _S("أزرق بحدود بيضاء", "#0A84FF", "#FFD60A", "#FFFFFF", 9),
    "orange": _S("برتقالي", "#FF9500", "#FFFFFF", "#000000", 9),
}

FILTERS = {
    "cine": ("سينمائي", "eq=contrast=1.08:saturation=0.9,colorbalance=rs=.04:bs=-.04"),
    "warm": ("دافئ", "eq=saturation=1.15,colorbalance=rs=.06:bs=-.06"),
    "vivid": ("حيوي", "eq=contrast=1.1:saturation=1.35"),
    "bw": ("أبيض وأسود", "hue=s=0,eq=contrast=1.1"),
    "cool": ("بارد", "eq=saturation=1.05,colorbalance=rs=-.05:bs=.07"),
    "matte": ("باهت ناعم", "eq=contrast=0.9:brightness=0.03:saturation=0.85"),
    "punch": ("تباين عالٍ", "eq=contrast=1.25:saturation=1.1"),
    "bright": ("مُشرق", "eq=brightness=0.06:contrast=1.05:saturation=1.1"),
    "sunset": ("غروب", "eq=saturation=1.25,colorbalance=rs=.1:gs=-.02:bs=-.1"),
    "tealorange": ("تيل وبرتقالي", "colorbalance=rs=-.06:bs=.08:rh=.08:bh=-.06,eq=saturation=1.15"),
    "vintage": ("كلاسيكي", "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.05"),
    "moody": ("داكن درامي", "eq=contrast=1.15:brightness=-0.05:saturation=0.8"),
    "soft": ("ناعم", "eq=contrast=0.95:saturation=0.95,gblur=sigma=0.6"),
    "sharp": ("حاد", "unsharp=5:5:1.0:5:5:0.0"),
    "clean": ("تحسين تلقائي", "eq=contrast=1.06:saturation=1.12:brightness=0.02,unsharp=3:3:0.5"),
}


def ensure_sfx():
    """يولّد مؤثرات أساسية تلقائياً؛ وأي ملف تضعه في sfx/ أو music/ يظهر في الصفحة."""
    SFX.mkdir(exist_ok=True)
    MUSIC.mkdir(exist_ok=True)
    ICONS.mkdir(exist_ok=True)
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
    if "step" in kw:
        kw["since"] = time.time()
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


CTX = threading.local()  # مفاتيح المهمة الحالية لكل خيط
PROV = {  # مزوّدون متوافقون مع واجهة OpenAI: (العنوان، ترتيب أفضلية الموديلات)
    "groq": ("https://api.groq.com/openai/v1", ["llama-3.3-70b", "llama-3.1-70b", "mixtral", "gemma2", "llama-3.1-8b"]),
    "cerebras": ("https://api.cerebras.ai/v1", ["llama-3.3-70b", "llama3.1-70b", "llama3.1-8b"]),
    "mistral": ("https://api.mistral.ai/v1", ["mistral-small", "mistral-large", "nemo", "pixtral"]),
    "github": ("https://models.github.ai/inference", ["gpt-4o-mini", "gpt-4o", "llama-3.3-70b", "phi-4"]),
    "openrouter": ("https://openrouter.ai/api/v1", [":free"]),
    "cohere": ("https://api.cohere.ai/compatibility/v1", ["command-r-plus", "command-a", "command-r"]),
}
FIXED = {"github": ["openai/gpt-4o-mini", "openai/gpt-4o", "meta/Llama-3.3-70B-Instruct", "microsoft/Phi-4"],
         "cohere": ["command-r-plus", "command-r"]}
# توزيع المهام: الأنسب والأوسع سياقاً أولاً (الافتراضي يعتمد على المجاني الأقوى)
ROUTE = {"pick": ["gemini", "openrouter", "github", "mistral", "cohere", "groq", "cerebras"],
         "director": ["gemini", "groq", "cerebras", "mistral", "github", "openrouter", "cohere"],
         "proof": ["groq", "cerebras", "gemini", "mistral", "github", "openrouter", "cohere"],
         "meta": ["groq", "cerebras", "mistral", "gemini", "github", "openrouter", "cohere"]}
BAD_M = ("whisper", "guard", "embed", "tts", "vision", "moderation", "rerank", "audio", "image", "ocr", "code")
_PM = {}


def clean_extra(d):
    return {k: str(v).strip() for k, v in (d or {}).items() if k in PROV and str(v).strip()}


def clean_route(d):
    return {t: p for t, p in (d or {}).items() if t in ROUTE and (p in PROV or p == "gemini")}


def parse_extra(txt):
    try:
        return clean_extra(json.loads(txt)) if txt.strip() else {}
    except Exception:
        return {}


def parse_route(txt):
    try:
        return clean_route(json.loads(txt)) if txt.strip() else {}
    except Exception:
        return {}


def parse_json_loose(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    try:
        return json.loads(txt)
    except Exception:
        return json.loads(re.search(r"(\[.*\]|\{.*\})", txt, re.S).group(1))


def openai_json(prov, key, prompt):
    base, pref = PROV[prov]
    h = {"Authorization": f"Bearer {key}"}
    cands = [_PM[(prov, key)]] if (prov, key) in _PM else []
    if not cands:  # اكتشاف الموديلات المتاحة واختيار الأنسب تلقائياً
        try:
            data = requests.get(f"{base}/models", headers=h, timeout=30).json()
            ids = [m.get("id") or m.get("name") for m in ((data.get("data") if isinstance(data, dict) else data) or [])]
            ids = [i for i in ids if i and not any(b in i.lower() for b in BAD_M)]
            cands = sorted(ids, key=lambda i: next((n for n, q in enumerate(pref) if q in i.lower()), 99))[:3]
        except Exception:
            pass
        cands = cands or FIXED.get(prov, [])
    err = ""
    for m in cands:
        try:
            d = requests.post(f"{base}/chat/completions", headers=h, timeout=180, json={
                "model": m, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]}).json()
            if "choices" in d:
                _PM[(prov, key)] = m
                return parse_json_loose(d["choices"][0]["message"]["content"])
            err = str(d)[:160]
        except Exception as ex:
            err = str(ex)[:160]
    _PM.pop((prov, key), None)
    raise RuntimeError(f"{prov}: {err or 'لا موديل صالح'}")


def llm_json(task, prompt, gkey="", gmodel=""):
    """يجرّب المزوّدين بالترتيب المناسب للمهمة، وينتقل للتالي عند أي فشل."""
    k = getattr(CTX, "keys", None) or {}
    ex = dict(k.get("extra") or {})
    if k.get("groq"):
        ex["groq"] = k["groq"]
    gk = gkey or k.get("gemini", "")
    pref = (k.get("route") or {}).get(task)
    order = ([pref] if pref else []) + [x for x in ROUTE[task] if x != pref]
    errs = []
    for prov in order:
        try:
            if prov == "gemini":
                if gk:
                    return gemini_json(gk, prompt, gmodel or k.get("model", ""))
            elif ex.get(prov):
                return openai_json(prov, ex[prov], prompt)
        except Exception as e:
            errs.append(f"{prov}: {str(e)[:110]}")
    raise RuntimeError("تعذّر على كل المزوّدين: " + " | ".join(errs) if errs else "أضف مفتاح مزوّد ذكاء اصطناعي واحداً على الأقل")


def as_list(x):
    return next((v for v in x.values() if isinstance(v, list)), []) if isinstance(x, dict) else x


EMOJI_DICT = {
    "مال": "💰", "فلوس": "💰", "نقود": "💰", "ثروة": "💰", "ربح": "📈", "أرباح": "📈", "ارباح": "📈", "دولار": "💵", "دولارات": "💵",
    "ذهب": "🥇", "نجاح": "🏆", "فوز": "🏆", "حب": "❤️", "قلب": "❤️", "نار": "🔥", "فكرة": "💡", "تحذير": "⚠️", "انتبه": "⚠️",
    "وقت": "⏰", "ساعة": "⏰", "سر": "🤫", "أسرار": "🤫", "اسرار": "🤫", "مفاجأة": "😮", "ضحك": "😂", "هاتف": "📱", "جوال": "📱",
    "فيديو": "🎬", "كتاب": "📚", "علم": "🎓", "قوة": "💪", "سيارة": "🚗", "طائرة": "✈️", "بيت": "🏠", "منزل": "🏠", "عمل": "💼",
    "شركة": "🏢", "بنك": "🏦", "خسارة": "📉", "مشكلة": "❌", "خطأ": "❌", "صح": "✅", "تجارة": "🛒", "استثمار": "📊",
    "money": "💰", "cash": "💰", "profit": "📈", "growth": "📈", "dollar": "💵", "gold": "🥇", "success": "🏆", "win": "🏆",
    "love": "❤️", "fire": "🔥", "idea": "💡", "warning": "⚠️", "time": "⏰", "secret": "🤫", "phone": "📱", "video": "🎬",
    "book": "📚", "loss": "📉", "invest": "📊", "business": "💼"}


def stems(x):
    """الكلمة وجذوعها المحتملة بإزالة بادئة واحدة (ال، وال، بال، ل، ف...)."""
    out = {x}
    m = re.sub(r"^[وفبلك]?ال", "", x)
    if m != x and len(m) >= 2:
        out.add(m)
    m2 = re.sub(r"^[وفبلك]", "", x)
    if m2 != x and len(m2) >= 4:
        out.add(m2)
    return out


def emoji_for(text):
    x = re.sub(r"[^\w]", "", text.lower())
    return next((EMOJI_DICT[c] for c in stems(x) if c in EMOJI_DICT), None)


def fetch_yt_subs(jid, jdir, url):
    """إن كانت للفيديو ترجمة يدوية على يوتيوب نستعملها بدل التفريغ الآلي (أسرع وأصح نصاً)."""
    upd(jid, step="البحث عن ترجمة جاهزة في يوتيوب", progress=6)
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--skip-download", "--no-playlist", "--write-subs",
                        "--sub-langs", "ar.*,en.*", "--convert-subs", "srt", "-o", "yt", url],
                       cwd=jdir, capture_output=True, timeout=120)
        found = sorted(jdir.glob("yt.*.srt"), key=lambda f: (0 if ".ar" in f.name else 1, f.name))
        if found:
            shutil.copy(found[0], jdir / "subs.srt")
    except Exception:
        pass


def same_word(a, b):
    """تطابق الكلمة أو مشابهاتها (بادئات الجر والتعريف أو فارق إملائي بسيط)."""
    return a == b or (len(a) >= 3 and (bool(stems(a) & stems(b)) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.86))


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


def ask_gemini(key, lines, n, lo, hi, model="", src=""):
    prompt = (
        f"أنت محرر شورتس محترف. هذا تفريغ فيديو مع الثواني بين الأقواس.\n"
        f"اختر أفضل {n} مقاطع (أو أقل إن لم يوجد ما يستحق)، مدة كل مقطع بين {lo} و{hi} ثانية، "
        f"تبدأ عند بداية جملة وتنتهي عند نهاية فكرة مكتملة، دون تداخل بينها.\n"
        f"لكل مقطع: start و end بالثواني، score من 1 إلى 10، hook (جملة افتتاحية تشد الانتباه "
        f"بحد أقصى 8 كلمات، بنفس لغة الفيديو)، title (عنوان قصير).\n"
        f"أرجع JSON فقط: مصفوفة من الكائنات.\n\n" + "\n".join(lines)
    )
    if src:
        prompt = f"عنوان الفيديو الأصلي: {src}. اجعل عنوان كل مقطع جزءاً مكمّلاً له ويخدم عنوان الأصل.\n" + prompt
    clips = as_list(llm_json("pick", prompt, key, model))
    return [c for c in clips if float(c["end"]) > float(c["start"])]


def pick_clips(key, words, n, lo, hi, model="", src=""):
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
            found += ask_gemini(key, part, n if size > total else min(n, 5), lo, hi, model, src)
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
        r = llm_json("meta", "اكتب لكل شورت وصفاً من سطرين و5 هاشتاجات بنفس لغة العنوان. "
                     'أرجع JSON بالشكل {"items":[{"description":"","hashtags":[""]}]} بنفس ترتيب المدخلات:\n'
                     + json.dumps(items, ensure_ascii=False))
        return r.get("items", []) if isinstance(r, dict) and "items" in r else as_list(r)
    except Exception:
        return [{"description": "", "hashtags": []} for _ in clips]


def ts(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def build_ass(words, start, end, hook, font, st, emph=(), banner="", emos=(), anim="pop", wordfx="pop", bsize=44, ovl=()):
    head = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,{font},{st['size']},{st['fg']},{st['fg']},{st['ol']},&H80000000,1,0,0,0,100,100,0,0,1,{st['bw']},2,2,60,60,{st['mv']},1
Style: Hook,{font},100,{st['hl']},{st['hl']},&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,3,8,70,70,260,1
Style: Banner,{font},{bsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,1,0,0,0,100,100,0,0,3,10,0,8,40,40,60,1
Style: Emoji,Segoe UI Emoji,150,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,40,40,380,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    ev, pops = [], []
    clean = lambda t: t.replace("{", "").replace("}", "")
    dur = end - start
    ev.append(f"Dialogue: 1,{ts(0)},{ts(min(3.0, dur))},Hook,,0,0,0,,"
              r"{\q0\fad(150,250)\fscx60\fscy60\t(0,250,\fscx100\fscy100)}" + clean(hook))
    ws = [w for w in words if w["start"] / 1000 >= start and w["end"] / 1000 <= end]
    if banner:
        ev.append(rf"Dialogue: 2,{ts(0)},{ts(end - start)},Banner,,0,0,0,,{{\q0}}" + clean(banner))
    for t0, em_ in emos:
        ev.append(rf"Dialogue: 3,{ts(t0)},{ts(min(t0 + 1.3, end - start))},Emoji,,0,0,0,,"
                  rf"{{\fad(80,200)\fscx40\fscy40\t(0,200,\fscx100\fscy100)}}" + clean(em_))
    for e_ in ovl:
        col = bgr(e_["c"])
        if e_["k"] == "t":
            fn = "Segoe UI Emoji" if e_.get("emo") else font
            ev.append(rf"Dialogue: 5,{ts(0)},{ts(end - start)},Cap,,0,0,0,,{{\an5\pos({e_['x'] * 10.8:.0f},{e_['y'] * 19.2:.0f})"
                      rf"\fn{fn}\fs{e_['sz']}\c{col}&\3c&H000000&\bord4}}" + clean(e_["tx"]))
        else:
            path = "m " + " l ".join(f"{a} {b}" for a, b in e_["p"])
            ev.append(rf"Dialogue: 5,{ts(0)},{ts(end - start)},Cap,,0,0,0,,{{\an7\pos(0,0)\p1\bord{e_['w']}"
                      rf"\3c{col}&\1a&HFF&\shad0}}" + path)
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
                sc = 100 + (j % 2)  # فرق 1% يجعل كل كلمة جزءاً مستقلاً
                fn = "\\fn" + ("Segoe UI Emoji" if x.get("emo") else font)
                reset = "\\3c" + st["ol"] + "&\\bord" + str(st["bw"]) + "\\u0\\blur0" + fn
                if j == k and st.get("mode") != "allbox":
                    pp = st["pop"]
                    extra = {"bounce": "\\t(0,90,\\fscx%d\\fscy%d)\\t(90,180,\\fscx%d\\fscy%d)" % (pp + 12, pp + 12, pp, pp),
                             "underline": "\\u1", "glow": "\\3c" + st["hl"] + "&\\bord" + str(st["bw"] + 5) + "\\blur1",
                             "box": "\\3c" + st["hl"] + "&\\bord" + str(st["bw"] + 8)}.get(wordfx, "")
                    col = "\\c&H00000000&" if wordfx == "box" else "\\c" + st["hl"] + "&"
                    if st.get("mode") == "wordbox":
                        extra += "\\3c" + st["bx"] + "&\\bord12"
                        col = "\\c" + st["hl"] + "&"
                    tag = "{" + reset + col + "\\fscx%d\\fscy%d" % (pp, pp) + extra + "}"
                elif norm(x["text"]) in em:
                    tag = "{" + reset + "\\c&H0000A5FF&\\fscx%d\\fscy%d}" % (sc, sc)
                else:
                    tag = "{" + reset + "\\c" + st["fg"] + "&\\fscx%d\\fscy%d}" % (sc, sc)
                runs.append(tag + clean(x["text"]))
            if rtl:
                runs.reverse()  # libass يضع الأجزاء من اليسار لليمين، فنعكس الترتيب ليُقرأ من اليمين
            y0 = 1920 - st["mv"]
            pos = "{\\an2\\pos(540,%d)}" % y0
            lead = {"slide": "{\\an2\\move(540,%d,540,%d,0,140)\\fad(70,0)}" % (y0 + 50, y0) if k == 0 else pos,
                    "drop": "{\\an2\\move(540,%d,540,%d,0,140)\\fad(70,0)}" % (y0 - 50, y0) if k == 0 else pos}.get(anim, "")
            ev.append(f"Dialogue: 0,{ts(max(a, 0))},{ts(max(b, a + 0.05))},Cap,,0,0,0,,{lead}{' '.join(runs)}")
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


def render(jdir, i, c, words, o, final=False):
    lowres = bool(o.get("preview")) and not final  # معاينة سريعة 540p
    outf = f"out/final{i}.mp4" if final else f"out/clip{i}.mp4"
    st = dict(o["cap_def"]) if o.get("cap") == "custom" and o.get("cap_def") else dict(CAP_STYLES.get(o["cap"], CAP_STYLES["gold"]))
    st["size"] = int(st["size"] * float(o.get("cap_scale", 1)))
    st["mv"] = int(1920 * float(o.get("cap_pos", 27)) / 100)
    s, e = float(c["start"]), float(c["end"])
    src_d = e - s
    segs, mw = plan_cuts(words, s, e, o["cut"])
    d = sum(b - a for a, b in segs)  # المدة بعد حذف الصمت
    plan = c.get("plan") or {}
    byfile, evs, off, emos, rfilt, icons = {}, [], 0.0, [], [], []

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
            if e_.get("emoji") and mw:  # الإيموجي يحلّ محل الكلمة الهدف داخل الكابشنز
                k_ = min(range(len(mw)), key=lambda q: abs(mw[q]["start"] / 1000 - nt))
                if abs(mw[k_]["start"] / 1000 - nt) < 0.8:
                    mw[k_]["text"], mw[k_]["emo"] = e_["emoji"], True
            if e_.get("sfx") and (SFX / e_["sfx"]).is_file():
                byfile.setdefault(e_["sfx"], []).append(nt)
    for rl in o.get("rules", []):  # قواعد الكلمات: إيموجي/مؤثر/تأثير عند ظهور الكلمة
        kw = re.sub(r"[^\w]", "", str(rl.get("word", "")).lower())
        for w_ in mw:
            if kw and same_word(re.sub(r"[^\w]", "", w_["text"].lower()), kw):
                tt = w_["start"] / 1000
                if rl.get("emoji"):
                    w_["text"], w_["emo"] = rl["emoji"], True
                if rl.get("fx") in ("punch", "flash", "burst") and tt < d - 0.3:
                    evs.append((tt, rl["fx"], ""))
                if rl.get("sfx") and (SFX / rl["sfx"]).is_file():
                    byfile.setdefault(rl["sfx"], []).append(tt)
                if rl.get("filter") in FILTERS and tt < d - 0.3:
                    rfilt.append((tt, rl["filter"]))
                if rl.get("icon") and (ICONS / rl["icon"]).is_file() and tt < d - 0.3:
                    icons.append((tt, ICONS / rl["icon"]))
    if o.get("emoji_auto"):  # قاموس الكلمات الشائعة (مال، ربح، دولار...) بحد أقصى إيموجي كل 2.5 ثانية
        last = -9.0
        for w_ in mw:
            if w_.get("emo"):
                last = w_["start"] / 1000
            elif w_["start"] / 1000 - last >= 2.5 and emoji_for(w_["text"]):
                w_["text"], w_["emo"] = emoji_for(w_["text"]), True
                last = w_["start"] / 1000
    ass, pops = build_ass(mw, 0.0, d, c["hook"], o["font"], st, plan.get("emphasis", []), o.get("banner", ""), emos,
                         o.get("anim", "pop"), o.get("wordfx", "pop"), int(o.get("banner_size", 44)), o.get("overlays", []))
    (jdir / f"clip{i}.ass").write_text(ass, encoding="utf-8")

    # 1) قص 9:16 (مع تتبع الوجه) على الزمن الأصلي
    tr = face_track(jdir / "src.mp4", s, e) if o["track"] and o.get("layout") not in ("blur", "top", "free") else None
    ce = crop_expr(*tr, src_d) if tr else None
    pre = (f"crop={ce[0]}:{ce[1]}:x='{ce[2]}':y=0" if ce else "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'")
    pre += ",scale=1080:1920:flags=lanczos,unsharp=5:5:0.7:5:5:0.0,setsar=1"
    fr = o.get("free") if o.get("layout") == "free" else None
    blur = o.get("layout") in ("blur", "top")
    oy = "140" if o.get("layout") == "top" else "(H-h)/2"
    if fr:
        asp = {"sq": "1", "45": "4/5"}.get(fr["asp"])
        cr = f"crop='min(iw,ih*{asp})':'min(ih,iw/({asp}))'," if asp else ""
        bd = f",drawbox=x=0:y=0:w=iw:h=ih:color=0x{fr['bc'][1:]}:t={fr['bw']}" if fr["bw"] else ""
        bgp = (["[0:v]split=2[bgi][fgi]", "[bgi]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=32:6[bgb]"]
               if fr["bgm"] == "blur" else [f"color=c=0x{fr['bgc'][1:]}:s=1080x1920:r=30:d={src_d:.2f}[bgb]", "[0:v]null[fgi]"])
        pre_parts = bgp + [f"[fgi]{cr}scale={int(1080 * fr['s'] / 100) // 2 * 2}:-2:flags=lanczos{bd}[fgs]",
                           f"[bgb][fgs]overlay=x='{fr['x'] * 10.8:.1f}-w/2':y='{fr['y'] * 19.2:.1f}-h/2',setsar=1[cv]"]
    else:
      pre_parts = ([ "[0:v]split=2[bgi][fgi]",
                   "[bgi]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=32:6[bgb]",
                   "[fgi]scale=1080:-2:flags=lanczos[fgs]",
                   f"[bgb][fgs]overlay=(W-w)/2:{oy},setsar=1[cv]"] if blur else [f"[0:v]{pre}[cv]"])

    # 2) بعد حذف الصمت: تقريب + انتقال + كابشنز على الزمن الجديد
    post = []
    base = {"slow": f"1+0.08*t/{d:.2f}", "pulse": "1+0.04*abs(sin(PI*t/1.4))"}.get(o["zoom"])
    bumps = "".join(f"+gte(t,{t0:.2f})*lt(t,{t0 + 0.7:.2f})*0.09*sin(PI*(t-{t0:.2f})/0.7)"
                    for t0, fx, _ in evs if fx == "punch") + "".join(
        f"+gte(t,{t0:.2f})*lt(t,{t0 + 0.4:.2f})*0.2*(1-(t-{t0:.2f})/0.4)" for t0, fx, _ in evs if fx == "burst")
    if o.get("trans") in ("zoomin", "whip"):  # دخول بتقريب سريع يستقر
        bumps += "+0.25*max(0,1-t/0.45)" + (f"+0.12*max(0,(t-{d - 0.4:.2f})/0.4)" if o["trans"] == "zoomin" else "")
        if o["trans"] == "whip":
            evs.append((0.0, "flash", ""))
    zoom = ((base or "1") + bumps) if (base or bumps) else None
    fx = {"fade": ("black", 0.3), "flash": ("white", 0.18), "fadewhite": ("white", 0.4)}.get(o["trans"])
    if zoom:
        post.append(f"scale=w='trunc(1080*({zoom})/2)*2':h=-2:eval=frame,crop=1080:1920")
    if fx:
        col, fd = fx
        post.append(f"fade=t=in:st=0:d={fd}:color={col},fade=t=out:st={d - fd:.2f}:d={fd}:color={col}")
    if o.get("filter") in FILTERS:
        post.append(FILTERS[o["filter"]][1])
    for tt, key_ in rfilt:  # فلتر مؤقت عند الكلمة المحددة
        post.append(",".join(f"{x}:enable='between(t,{tt:.2f},{tt + 1.6:.2f})'" for x in FILTERS[key_][1].split(",")))
    for t0, fx, _ in evs:
        if fx == "flash":
            post.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t,{t0:.2f},{t0 + 0.1:.2f})'")
    post.append(f"ass=clip{i}.ass")

    n = len(segs)
    parts = pre_parts + [
             f"[cv]split={n}" + "".join(f"[c{k}]" for k in range(n)),
             f"[0:a]asplit={n}" + "".join(f"[x{k}]" for k in range(n))]
    for k, (a, b) in enumerate(segs):
        zj = ",scale=1210:-2,crop=1080:1920,setsar=1" if (o.get("cutzoom", True) and n > 1 and k % 2) else ""
        parts.append(f"[c{k}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS{zj}[sv{k}]")
        parts.append(f"[x{k}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[sa{k}]")
    parts.append("".join(f"[sv{k}][sa{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{','.join(post[:-1]) or 'null'}[vg]")  # كل شيء قبل الكابشنز
    vg, gr = "[vg]", o.get("grad") or {}
    if gr:  # طبقة لون متدرجة شفافة تحت الكابشنز
        hh = 1920 * gr["h"] / 100
        tt = f"clip((Y-{1920 - hh:.0f})/{hh:.0f},0,1)" if gr["pos"] == "bottom" else f"clip(1-Y/{hh:.0f},0,1)"
        pw = {"linear": 1, "smooth": 2.2, "steep": 0.5}[gr["shape"]]
        R, G, B = (int(gr["col"][i:i + 2], 16) for i in (1, 3, 5))
        parts.append(f"color=c=black:s=1080x1920:r=30:d={d:.2f},format=gbrap,"
                     f"geq=r={R}:g={G}:b={B}:a='255*{gr['op']}*pow({tt},{pw})',format=rgba[gl]")
        parts.append("[vg][gl]overlay=0:0:format=auto[vg2]")
        vg = "[vg2]"
    parts.append(f"{vg}{post[-1]}[vp]")
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
    if o.get("trsfx") and o.get("trans"):
        add_sfx(o["trsfx"], [0.0, max(d - 0.4, 0.0)], 0.8)
    if o.get("zmsfx") and o.get("zoom"):
        add_sfx(o["zmsfx"], [0.0], 0.8)
    if o["sfx_chunk"] and pops:
        add_sfx(o["sfx_chunk"], pops, 0.5)
    for name, times in byfile.items():  # مؤثرات اختارها المخرج الذكي
        add_sfx(name, times, 0.75)
    if o["music"]:
        cmd += ["-stream_loop", "-1", "-i", str(MUSIC / o["music"])]
        parts.append(f"[{idx}:a]volume={o['music_vol']}[m]")
        labels.append("[m]")
        idx += 1
    vsrc = "[vp]"
    for k, vz in enumerate(plan.get("visuals", []) if o.get("visuals") else []):
        nt = rt(float(vz["t"]))
        if not vz.get("file") or not (jdir / vz["file"]).is_file() or nt is None or nt >= d - 0.5:
            continue
        du = float(vz["dur"])
        cmd.extend(["-loop", "1", "-framerate", "30", "-t", f"{nt + du + 1:.2f}", "-i", str(jdir / vz["file"])])
        parts.append(f"[{idx}:v]scale=600:-2,format=rgba,fade=t=in:st=0:d=0.3:alpha=1,setpts=PTS+{nt:.2f}/TB[im{k}]")
        parts.append(f"{vsrc}[im{k}]overlay=(W-w)/2:330:enable='between(t,{nt:.2f},{nt + du:.2f})':eof_action=pass[vv{k}]")
        vsrc, idx = f"[vv{k}]", idx + 1
    for k, (tt, fp) in enumerate(icons[:12]):  # أيقونات PNG ملوّنة تظهر فوق الكابشنز عند الكلمة
        cmd.extend(["-loop", "1", "-framerate", "30", "-t", f"{tt + 1.8:.2f}", "-i", str(fp)])
        parts.append(f"[{idx}:v]scale=190:-2,format=rgba,fade=t=in:st=0:d=0.15:alpha=1,setpts=PTS+{tt:.2f}/TB[ic{k}]")
        parts.append(f"{vsrc}[ic{k}]overlay=x=(W-w)/2:y={1920 - st['mv'] - 340}:enable='between(t,{tt:.2f},{tt + 1.4:.2f})':eof_action=pass[iv{k}]")
        vsrc, idx = f"[iv{k}]", idx + 1
    parts.append(f"{vsrc}scale=540:960:flags=fast_bilinear[vo]" if lowres else f"{vsrc}null[vo]")
    m = len(labels)
    parts.append("".join(labels) + f"amix=inputs={m}:duration=first:dropout_transition=0,volume={m},"
                 f"afade=t=in:d=0.2,afade=t=out:st={d - 0.3:.2f}:d=0.3[ao]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vo]", "-map", "[ao]", "-t", f"{d:.2f}",
            "-threads", str(max(2, (os.cpu_count() or 4) - 1)),
            "-r", "30", "-c:v", "libx264", "-preset", "ultrafast" if lowres else ("slow" if o.get("hq", True) else "veryfast"), "-crf", "30" if lowres else "16",
            "-maxrate", "3M" if lowres else "16M", "-bufsize", "6M" if lowres else "32M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", outf]
    sh(cmd, jdir)
    # صورة مصغرة خفيفة للعرض في الصفحة بدل تحميل الفيديو كاملاً
    if not final:
        sh(["ffmpeg", "-y", "-ss", "0.8", "-i", f"out/clip{i}.mp4", "-frames:v", "1", "-vf", "scale=360:-2",
            f"out/clip{i}.jpg"], jdir)


def parse_srt(txt):
    """يقرأ SRT أو VTT ويعيد [(بداية، نهاية، نص)] بالثواني."""
    cues = []
    pat = (r"(?:(\d+):)?(\d+):(\d+)[,.](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[,.](\d+)[^\n]*\n(.*?)(?:\n\s*\n|\Z)")
    for m in re.finditer(pat, txt.replace("\r", ""), re.S):
        g = m.groups()
        a = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")[:3]) / 1000
        b = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")[:3]) / 1000
        text = re.sub(r"<[^>]+>", "", g[8]).replace("\n", " ").strip()
        if text and b > a:
            cues.append((a, b, text))
    return cues


def words_from_cues(cues):
    """توقيت الكلمات موزّع بالتساوي داخل كل سطر ترجمة (دون حاجة لتفريغ آلي)."""
    out = []
    for a, b, text in cues:
        tk = text.split()
        step = (b - a) / len(tk)
        out += [{"text": t, "start": int((a + i * step) * 1000), "end": int((a + (i + 1) * step) * 1000)}
                for i, t in enumerate(tk)]
    return out


def align_cues(asr, cues):
    """نص الترجمة الصحيح بتوقيت التفريغ الآلي الدقيق للكلمات."""
    out = []
    for a, b, text in cues:
        old = [w for w in asr if a * 1000 <= (w["start"] + w["end"]) / 2 < b * 1000]
        out += apply_edit(old, text.split()) if old else words_from_cues([(a, b, text)])
    return out


def download_video(jid, jdir, url):
    """تنزيل يوتيوب بعدة محاولات: أجزاء صغيرة، ثم تحديث yt-dlp، ثم ملف واحد."""
    base = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--retries", "20", "--fragment-retries", "20",
            "--socket-timeout", "30", "--merge-output-format", "mp4", "-o", "src.mp4"]
    js = shutil.which("deno") or shutil.which("node")
    if js:  # يوتيوب يحتاج بيئة JavaScript لكل الصيغ
        base += ["--js-runtimes", f"{Path(js).stem.lower()}:{js}"]
    tries = [["-f", "bv*[height<=1440]+ba/b[height<=1440]", "--http-chunk-size", "10M"],
             ["-f", "bv*[height<=1080]+ba/b[height<=1080]", "--http-chunk-size", "4M"],
             ["-f", "b[height<=720]/b", "--http-chunk-size", "4M"]]
    errs = []
    for i, extra in enumerate(tries):
        if i == 2:  # قبل المحاولة الأخيرة: يوتيوب يتغير باستمرار فنحدّث الأداة
            upd(jid, step="تحديث yt-dlp ثم إعادة المحاولة", progress=5)
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"], capture_output=True, timeout=300)
            except Exception:
                pass
        upd(jid, step=f"تنزيل الفيديو (محاولة {i + 1} من {len(tries)})", progress=4)
        try:
            sh(base + extra + [url], jdir)
            return
        except RuntimeError as ex:
            errs.append(str(ex)[-220:])
    raise RuntimeError("تعذّر تنزيل الفيديو بعد عدة محاولات. جرّب: (1) pip install -U \"yt-dlp[default]\" "
                       "(2) winget install DenoLand.Deno ثم أعد فتح الطرفية (3) أو نزّل الفيديو بنفسك وارفعه من صفحة الإعدادات.\n"
                       "آخر خطأ: " + errs[-1])


def stage1(jid, keys, n, lo, hi, o):
    jdir = JOBS_DIR / jid
    (jdir / "out").mkdir(exist_ok=True)
    if o["url"] and not (jdir / "src.mp4").exists():
        download_video(jid, jdir, o["url"])
    wf, sf = jdir / "words.json", jdir / "subs.srt"
    if o["url"] and not sf.exists() and not wf.exists():
        fetch_yt_subs(jid, jdir, o["url"])
    if wf.exists():  # استكمال بعد عطل: لا نعيد التفريغ
        words = json.loads(wf.read_text(encoding="utf-8"))
    else:
        cues = parse_srt(sf.read_text(encoding="utf-8-sig", errors="ignore")) if sf.exists() else []
        if cues and not (keys["assembly"] and o.get("precise")):  # الترجمة الجاهزة تغني عن التفريغ الآلي
            upd(jid, step="قراءة ملف الترجمة", progress=15)
            words = words_from_cues(cues)
        else:
            if not keys["assembly"]:
                raise RuntimeError("أضف مفتاح AssemblyAI أو ارفع ملف ترجمة")
            upd(jid, step="استخراج الصوت", progress=8)
            sh(["ffmpeg", "-y", "-i", "src.mp4", "-vn", "-ac", "1", "-ar", "16000", "audio.mp3"], jdir)
            upd(jid, step="التفريغ النصي (AssemblyAI)", progress=15)
            words = [{"text": w["text"], "start": w["start"], "end": w["end"]}
                     for w in transcribe(keys["assembly"], jdir / "audio.mp3")]
            if cues:
                words = align_cues(words, cues)
        wf.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    upd(jid, step="اختيار أقوى المقاطع (Gemini)", progress=40)
    clips = pick_clips(keys["gemini"], words, max(n, 10) if o.get("suggest") else n, lo, hi,
                       keys["model"], o.get("src_title", ""))
    return clips, words


def worker(jid, keys, n, lo, hi, o, resume=None):
    CTX.keys = keys
    jdir = JOBS_DIR / jid
    try:
        if resume:
            clips, words = resume
        else:
            clips, words = stage1(jid, keys, n, lo, hi, o)
            if o.get("suggest"):  # وضع الاقتراح: توقف ليختار المستخدم
                for k, c in enumerate(clips):
                    c["i"] = k
                    c["caption"] = " ".join(w["text"] for w in words if float(c["start"]) <= w["start"] / 1000
                                            and w["end"] / 1000 <= float(c["end"]))[:160]
                upd(jid, status="suggested", step="اختر المقاطع التي تريد تصديرها", progress=100, candidates=clips)
                save_project(jid)
                return
        src = o.get("src_title", "")
        if o.get("numbering") and src:  # ترقيم الشورتس بعد عنوان الأصل
            for k, c in enumerate(clips):
                if not str(c["title"]).startswith(src):
                    c["title"] = f"{src} {k + 1} - {c['title']}"
        if o.get("proof") and clips:
            upd(jid, step="التدقيق اللغوي للكابشنز (Gemini)", progress=44)
            try:
                idxs = [[k for k, w in enumerate(words) if float(c["start"]) <= w["start"] / 1000
                         and w["end"] / 1000 <= float(c["end"])] for c in clips]
                fixed = proofread(keys["gemini"], keys["model"], [" ".join(words[k]["text"] for k in ix) for ix in idxs])
                for ix, txt in reversed(list(zip(idxs, fixed))):  # من الأخير حتى لا تتغير الفهارس
                    if ix and isinstance(txt, str) and txt.split():
                        words[ix[0]:ix[-1] + 1] = apply_edit([words[k] for k in ix], txt.split())
                (jdir / "words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
            except Exception as ex:
                print("تحذير: تعذّر التدقيق اللغوي:", ex)
        if o.get("auto_fx"):
            upd(jid, step="المخرج الذكي: تأثيرات وأصوات (Gemini)", progress=46)
            try:
                names = sorted(p.name for p in SFX.iterdir() if p.suffix.lower() in AUDIO_EXT)
                for c, pl in zip(clips, direct(keys["gemini"], keys["model"], words, clips, names)):
                    c["plan"] = clean_plan(pl, float(c["end"]) - float(c["start"]), names)
                    if o.get("visuals"):
                        for k, vz in enumerate(c["plan"]["visuals"]):
                            fn = f"v{int(float(c['start']) * 10)}_{k}.png"
                            if gen_image(keys["gemini"], vz["prompt"], jdir / fn):
                                vz["file"] = fn
            except Exception as ex:
                print("تحذير: تعذّر المخرج الذكي:", ex)
        upd(jid, step="كتابة الوصف والهاشتاجات (Groq)", progress=50)
        meta = write_meta("", clips)
        results = []
        for i, c in enumerate(clips):
            upd(jid, step=f"تجهيز الشورت {i + 1} من {len(clips)}", progress=55 + int(40 * i / len(clips)))
            render(jdir, i, c, words, o)
            m = meta[i] if i < len(meta) else {}
            results.append({"i": i, "plan": c.get("plan"), "final": "" if o.get("preview") else f"/jobs/{jid}/out/clip{i}.mp4", "thumb": f"/jobs/{jid}/out/clip{i}.jpg",
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
    if j["options"].get("preview"):  # أي تعديل يلغي النسخة النهائية القديمة
        (jdir / "out" / f"final{i}.mp4").unlink(missing_ok=True)
        c["final"] = ""
    save_project(jid)
    return c


_IMG = {}


def gen_image(key, prompt, path):
    """يولّد صورة عبر موديل صور Gemini المتاح لمفتاحك (إن وُجد)."""
    base = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        model = _IMG.get(key)
        if not model:
            r = requests.get(base, params={"key": key, "pageSize": 200}).json()
            names = [m["name"].split("/")[-1] for m in r.get("models", []) if "image" in m["name"]
                     and "imagen" not in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]
            if not names:
                return False
            model = _IMG[key] = sorted(names)[-1]
        r = requests.post(f"{base}/{model}:generateContent?key={key}", json={
            "contents": [{"parts": [{"text": "Simple flat icon illustration on a clean white background, no text: " + prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}).json()
        for p_ in r["candidates"][0]["content"]["parts"]:
            d_ = p_.get("inlineData") or p_.get("inline_data")
            if d_:
                path.write_bytes(base64.b64decode(d_["data"]))
                return True
    except Exception as ex:
        print("تحذير: تعذّر توليد الصورة:", ex)
    return False


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
        "- في events يمكن إضافة emoji (رمز واحد مناسب للمعنى) عند اللحظات المناسبة.\n"
        "- visuals (اختياري، حتى 2): عندما يذكر المتحدث مثالاً أو شيئاً ملموساً يمكن رسمه: t وdur (من 2 إلى 4 ثوانٍ) "
        "وprompt وصف إنجليزي قصير لأيقونة مبسطة.\n"
        'أرجع JSON فقط: مصفوفة بعدد المقاطع وبنفس ترتيبها، كل عنصر على الشكل '
        '{"events":[{"t":0.5,"fx":"punch","sfx":"whoosh.wav","emoji":"🔥"}],"emphasis":["كلمة"],"visuals":[{"t":3,"dur":3,"prompt":"a simple coin icon"}]}.\n\n' + "\n\n".join(items)
    )
    plans = as_list(llm_json("director", prompt, key, model))
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
                        "sfx": e.get("sfx") if e.get("sfx") in names else "", "emoji": str(e.get("emoji") or "")[:8]})
    evs.sort(key=lambda x: x["t"])
    out, last = [], -9.0
    for e in evs:
        if (e["fx"] or e["sfx"] or e["emoji"]) and e["t"] - last >= 1.5:
            out.append(e)
            last = e["t"]
    emph = [str(x) for x in p.get("emphasis")][:6] if isinstance(p.get("emphasis"), list) else []
    vis = []
    for v_ in (p.get("visuals") if isinstance(p.get("visuals"), list) else [])[:2]:
        try:
            t_, du = float(v_["t"]), min(max(float(v_.get("dur", 3)), 2.0), 4.0)
        except Exception:
            continue
        if v_.get("prompt") and 0 <= t_ < dur - du - 0.5:
            vis.append({"t": round(t_, 2), "dur": du, "prompt": str(v_["prompt"])[:200]})
    return {"events": out[:8], "emphasis": emph, "visuals": vis}


def proofread(key, model, texts):
    prompt = ("صحّح الأخطاء الإملائية والنحوية وعلامات الترقيم في كل نص دون تغيير المعنى أو إعادة الصياغة، "
              "وأبقِ عدد الكلمات وترتيبها قدر الإمكان. أرجع JSON فقط: مصفوفة نصوص بنفس العدد والترتيب.\n"
              + json.dumps(texts, ensure_ascii=False))
    r = as_list(llm_json("proof", prompt, key, model))
    return r if isinstance(r, list) and len(r) == len(texts) else texts


class Keys(BaseModel):
    assembly_key: str = ""
    gemini_key: str = ""
    extra: dict = {}
    route: dict = {}
    groq_key: str = ""
    model: str = ""


@app.post("/api/jobs/{jid}/retry")
def retry(jid: str, k: Keys):
    """يستكمل مهمة فشلت من حيث وصلت (التفريغ المحفوظ لا يُعاد)."""
    j = load_job(jid)
    if not j or j.get("status") != "error":
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    o = j["options"]
    keys = {"assembly": k.assembly_key, "gemini": k.gemini_key, "groq": k.groq_key,
            "model": re.sub(r"[^\w.\-]", "", k.model),
            "extra": clean_extra(k.extra), "route": clean_route(k.route)}
    j.update(status="running", error="")
    upd(jid, step="استكمال المعالجة", progress=5)
    threading.Thread(target=worker, args=(jid, keys, o.get("n", 5), o.get("lo", 25), o.get("hi", 60), o),
                     daemon=True).start()
    return {"id": jid}


class Pick(BaseModel):
    picks: list[int]
    gemini_key: str = ""
    extra: dict = {}
    route: dict = {}
    groq_key: str = ""
    model: str = ""


@app.post("/api/jobs/{jid}/render")
def render_selected(jid: str, p: Pick):
    j = load_job(jid)
    if not j or j.get("status") != "suggested":
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    clips = [j["candidates"][k] for k in p.picks if 0 <= k < len(j["candidates"])]
    if not clips:
        return JSONResponse({"error": "لا مقاطع"}, status_code=400)
    words = json.loads((JOBS_DIR / jid / "words.json").read_text(encoding="utf-8"))
    keys = {"gemini": p.gemini_key, "groq": p.groq_key, "model": re.sub(r"[^\w.\-]", "", p.model),
            "extra": clean_extra(p.extra), "route": clean_route(p.route)}
    j.update(status="running", step="بدء", progress=40, clips=[])
    threading.Thread(target=worker, args=(jid, keys, 0, 0, 0, j["options"], (clips, words)), daemon=True).start()
    return {"id": jid}


def _hex(v, default):
    return v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else default


def parse_grad(txt):
    try:
        g = json.loads(txt) if txt.strip() else {}
        if not g:
            return {}
        return {"col": _hex(g.get("col"), "#000000"), "op": min(max(float(g.get("op", 0.7)), 0.05), 1.0),
                "h": min(max(float(g.get("h", 35)), 5), 80), "pos": "top" if g.get("pos") == "top" else "bottom",
                "shape": g.get("shape") if g.get("shape") in ("linear", "smooth", "steep") else "smooth"}
    except Exception:
        return {}


def parse_capdef(txt):
    try:
        c = json.loads(txt) if txt.strip() else {}
        hx = c.get("hx", [])
        if not c or len(hx) != 3:
            return None
        return _S(str(c.get("label", "قالبي"))[:30], _hex(hx[0], "#FFFFFF"), _hex(hx[1], "#FFD700"), _hex(hx[2], "#000000"),
                  min(max(int(c.get("bw", 8)), 0), 20), n=min(max(int(c.get("n", 3)), 1), 5),
                  size=min(max(int(c.get("size", 84)), 40), 160),
                  mode=c.get("mode") if c.get("mode") in ("stroke", "wordbox", "allbox") else "stroke", bx=_hex(c.get("bx"), None))
    except Exception:
        return None


def parse_free(txt):
    try:
        f = json.loads(txt) if txt.strip() else {}
        if not f:
            return None
        return {"s": min(max(float(f.get("s", 100)), 20), 250), "x": min(max(float(f.get("x", 50)), -20), 120),
                "y": min(max(float(f.get("y", 50)), -20), 120), "asp": f.get("asp") if f.get("asp") in ("orig", "sq", "45") else "orig",
                "bw": min(max(int(f.get("bw", 0)), 0), 30), "bc": _hex(f.get("bc"), "#ffffff"),
                "bgm": "solid" if f.get("bgm") == "solid" else "blur", "bgc": _hex(f.get("bgc"), "#000000")}
    except Exception:
        return None


def parse_overlays(txt):
    try:
        out = []
        for e in (json.loads(txt) if txt.strip() else [])[:40]:
            c = _hex(e.get("c"), "#ffffff")
            if e.get("k") == "t" and str(e.get("tx", "")).strip():
                out.append({"k": "t", "tx": str(e["tx"])[:60], "x": min(max(float(e.get("x", 50)), 0), 100),
                            "y": min(max(float(e.get("y", 50)), 0), 100), "sz": min(max(int(e.get("sz", 90)), 20), 400),
                            "c": c, "emo": bool(e.get("emo"))})
            elif e.get("k") == "d" and len(e.get("p", [])) > 1:
                out.append({"k": "d", "p": [[int(a), int(b)] for a, b in e["p"][:400]], "c": c,
                            "w": min(max(int(e.get("w", 8)), 2), 40)})
        return out
    except Exception:
        return []


def parse_rules(txt):
    try:
        d = json.loads(txt) if txt.strip() else {}
        d = d.get("rules", []) if isinstance(d, dict) else d
        return [{"word": str(r.get("word", ""))[:40], "emoji": str(r.get("emoji", ""))[:16],
                 "sfx": Path(str(r.get("sfx", ""))).name, "fx": str(r.get("fx", "")), "filter": str(r.get("filter", ""))[:20], "icon": Path(str(r.get("icon", ""))).name}
                for r in d[:50] if isinstance(r, dict) and r.get("word")]
    except Exception:
        return []


@app.post("/api/jobs/{jid}/clips/{i}/export")
def export_clip(jid: str, i: int):
    """التصدير النهائي بجودة كاملة 1080×1920 لشورت واحد."""
    j = load_job(jid)
    if not j or not 0 <= i < len(j.get("clips", [])):
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    jdir = JOBS_DIR / jid
    words = json.loads((jdir / "words.json").read_text(encoding="utf-8"))
    c = j["clips"][i]
    try:
        render(jdir, i, {"start": float(c["start"]), "end": float(c["end"]), "hook": c["hook"], "title": c["title"],
                         "plan": c.get("plan")}, words, j["options"], final=True)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)
    c["final"] = f"/jobs/{jid}/out/final{i}.mp4"
    c["v"] = int(time.time())
    save_project(jid)
    return c


def pick(folder, name):
    n = Path(name).name if name else ""
    return n if n and (folder / n).is_file() else ""


@app.get("/api/assets")
def assets():
    ls = lambda d: sorted(p.name for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT)
    return {"sfx": ls(SFX), "music": ls(MUSIC), "caps": {k: {"label": v["label"], "hx": v["hx"], "bw": v["bw"], "n": v["n"], "size": v["size"],
                                    "mode": v["mode"], "bx": v["bxh"]}
                 for k, v in CAP_STYLES.items()},
            "filters": {k: v[0] for k, v in FILTERS.items()},
            "icons": sorted(p.name for p in ICONS.iterdir() if p.suffix.lower() in (".png", ".webp", ".jpg", ".jpeg"))}


@app.post("/api/jobs")
def create_job(video: UploadFile = File(None), subs: UploadFile = File(None), assembly_key: str = Form(""), gemini_key: str = Form(""),
               groq_key: str = Form(""), clips: int = Form(5), min_s: int = Form(25), max_s: int = Form(60),
               font: str = Form("Arial"), cap: str = Form("gold"), zoom: str = Form("slow"),
               trans: str = Form("fade"), sfx_start: str = Form(""), sfx_chunk: str = Form(""),
               music: str = Form(""), music_vol: float = Form(0.12), url: str = Form(""), track: str = Form("1"), cut: float = Form(0.45), gemini_model: str = Form(""), auto_fx: str = Form("1"), banner: str = Form(""),
               layout: str = Form("crop"), proof: str = Form("1"), hq: str = Form("1"),
               fx_filter: str = Form("", alias="filter"), suggest: str = Form("0"), anim: str = Form("pop"),
               visuals: str = Form("0"), cutzoom: str = Form("1"), cap_scale: float = Form(1.0), cap_pos: float = Form(27),
               banner_size: int = Form(44), wordfx: str = Form("pop"), rules: str = Form(""), numbering: str = Form("1"),
               grad: str = Form(""), cap_def: str = Form(""), overlays: str = Form(""), free: str = Form(""),
               trsfx: str = Form(""), zmsfx: str = Form(""), preview: str = Form("1"),
               extra_keys: str = Form(""), route: str = Form(""), precise: str = Form("0"), emoji_auto: str = Form("0")):
    if video is None and not url.startswith("http"):
        return JSONResponse({"error": "no video"}, status_code=400)
    jid = uuid.uuid4().hex[:10]
    jdir = JOBS_DIR / jid
    jdir.mkdir()
    if video is not None:
        with open(jdir / "src.mp4", "wb") as f:
            shutil.copyfileobj(video.file, f)
    if subs is not None:
        (jdir / "subs.srt").write_bytes(subs.file.read())
    jobs[jid] = {"status": "running", "step": "بدء", "progress": 2, "clips": [],
                 "name": (video.filename if video is not None else url)[:80], "created": time.strftime("%Y-%m-%d %H:%M")}
    keys = {"assembly": assembly_key, "gemini": gemini_key, "groq": groq_key,
            "model": re.sub(r"[^\w.\-]", "", gemini_model),
            "extra": parse_extra(extra_keys), "route": parse_route(route)}
    o = {"font": font, "cap": cap, "zoom": zoom, "trans": trans, "sfx_start": pick(SFX, sfx_start),
         "sfx_chunk": pick(SFX, sfx_chunk), "music": pick(MUSIC, music),
         "music_vol": round(min(max(music_vol, 0.0), 1.0), 2),
         "url": url if url.startswith("http") else "", "track": track == "1", "cut": round(min(max(cut, 0.0), 3.0), 2), "auto_fx": auto_fx == "1",
         "banner": banner[:80], "layout": layout if layout in ("crop", "blur", "top", "free") else "crop",
         "proof": proof == "1", "hq": hq == "1", "filter": fx_filter if fx_filter in FILTERS else "",
         "suggest": suggest == "1", "src_title": Path(video.filename).stem[:80] if video is not None else "",
         "anim": anim if anim in ("pop", "slide", "drop") else "pop", "visuals": visuals == "1", "cutzoom": cutzoom == "1",
         "cap_scale": min(max(cap_scale, 0.5), 1.8), "cap_pos": min(max(cap_pos, 5), 70),
         "banner_size": min(max(banner_size, 24), 90), "numbering": numbering == "1",
         "wordfx": wordfx if wordfx in ("pop", "bounce", "underline", "box", "glow") else "pop", "rules": parse_rules(rules), "grad": parse_grad(grad), "free": parse_free(free), "overlays": parse_overlays(overlays), "precise": precise == "1", "emoji_auto": emoji_auto == "1", "preview": preview == "1", "trsfx": pick(SFX, trsfx), "zmsfx": pick(SFX, zmsfx), "cap_def": parse_capdef(cap_def), "n": clips, "lo": min_s, "hi": max_s}
    jobs[jid]["options"] = o
    threading.Thread(target=worker, args=(jid, keys, clips, min_s, max_s, o), daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    j = load_job(jid)
    if not j:
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    return dict(j, wait=int(time.time() - j["since"])) if j.get("status") == "running" and "since" in j else j


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/jobs", StaticFiles(directory=JOBS_DIR), name="jobs")
app.mount("/sfx", StaticFiles(directory=SFX), name="sfx")
app.mount("/icons", StaticFiles(directory=ICONS), name="icons")
