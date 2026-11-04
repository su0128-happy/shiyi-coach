from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import feedparser
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = DATA / "reports"
EXPORTS = DATA / "exports"
PRACTICE_STORE = DATA / "practice_store.json"
REVIEW_STORE = DATA / "professional_reviews.json"
RECENT_QUESTION_STORE = DATA / "recent_questions.json"
FAVORITE_STORE = DATA / "favorites.json"
STATIC = ROOT / "static"
PROMPT = (ROOT / "coach_prompt.md").read_text(encoding="utf-8")

load_dotenv(ROOT / ".env")

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_COMPATIBLE_BASE_URL = os.getenv("DASHSCOPE_COMPATIBLE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
DASHSCOPE_ASR_MODEL = os.getenv("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")

TZ = timezone(timedelta(hours=8))

TOPICS = {
    "POL": "政治外交",
    "ECO": "经济金融",
    "TECH": "科技",
    "SOC": "社会",
    "ENV": "环境",
    "EDU": "教育健康",
    "BIZ": "商业",
    "CUL": "文体",
    "AUTO": "随机热点",
}

EN_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("Reuters", "https://news.google.com/rss/search?q=site:reuters.com+when:14d&hl=en-US&gl=US&ceid=US:en"),
]
ZH_FEEDS = [
    ("新华社", "https://news.google.com/rss/search?q=site:xinhuanet.com+when:14d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    ("人民日报", "https://news.google.com/rss/search?q=site:people.com.cn+when:14d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    ("澎湃", "https://news.google.com/rss/search?q=site:thepaper.cn+when:14d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    ("财新", "https://news.google.com/rss/search?q=site:caixin.com+when:14d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
]

FILLER_ZH = re.compile(r"(呃+|嗯+|那个|就是说?|然后呢?|啊+|这个这个)")
FILLER_EN = re.compile(r"\b(uh+|um+|erm+|like|you know|kinda|sort of)\b", re.I)
REPAIR_ZH = re.compile(r"(我是说|不对|重新|更正一下|不是|等一下)")
REPAIR_EN = re.compile(r"\b(I mean|wait|no wait|sorry|let me (?:rephrase|start again)|actually)\b", re.I)

app = FastAPI(title="视译训练教练")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class PracticeStartRequest(BaseModel):
    direction: str = "auto"
    topic: str = "AUTO"
    level: str = "C1"
    custom_text: str = ""


class PracticeReviewRequest(BaseModel):
    direction: str
    topic: str = "AUTO"
    level: str = "C1"
    questions: list[dict[str, Any]] = Field(default_factory=list)


class PracticeSaveRequest(BaseModel):
    direction: str
    topic: str = "AUTO"
    level: str = "C1"
    source: str = ""
    questions: list[dict[str, Any]] = Field(default_factory=list)
    review: dict[str, Any] = Field(default_factory=dict)


class ProfessionalReviewRequest(BaseModel):
    count: int = 3
    save: bool = False


class FavoriteRequest(BaseModel):
    record_id: str
    question_id: str
    note: str = ""


async def transcribe_audio(audio: bytes, content_type: str, direction: str) -> dict[str, Any]:
    if not DASHSCOPE_API_KEY or DASHSCOPE_API_KEY.startswith("sk-your"):
        raise HTTPException(503, "未配置 DASHSCOPE_API_KEY。请在 .env 配置新的阿里百炼 Key 后重启服务。")
    if not audio:
        raise HTTPException(422, "音频内容为空。")
    if len(audio) > 10 * 1024 * 1024:
        raise HTTPException(413, "单题音频不能超过 10MB。")
    mime_type = content_type.split(";")[0] if content_type else "audio/webm"
    encoded = base64.b64encode(audio).decode("ascii")
    payload = {
        "model": DASHSCOPE_ASR_MODEL,
        "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": f"data:{mime_type};base64,{encoded}"}}]}],
        "stream": False,
        "asr_options": {"language": "en" if direction == "EN-ZH" else "zh", "enable_itn": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
            response = None
            last_error = ""
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{DASHSCOPE_COMPATIBLE_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    if response.status_code < 500:
                        break
                    last_error = f"HTTP {response.status_code}"
                except httpx.RequestError as exc:
                    last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
            if response is None:
                raise HTTPException(502, f"阿里语音识别网络错误：{last_error}")
        if response.status_code >= 400:
            raise HTTPException(502, f"阿里语音识别失败：{response.status_code} {response.text[:300]}")
        data = response.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if isinstance(content, list):
            text = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content).strip()
        else:
            text = str(content).strip()
        if not text:
            raise HTTPException(502, "阿里语音识别未返回转写文本。")
        return {"transcript": text, "provider": "阿里百炼语音识别", "model": DASHSCOPE_ASR_MODEL}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"阿里语音识别网络错误：{exc}") from exc


def today() -> date:
    return datetime.now(TZ).date()


def default_direction() -> str:
    return "EN-ZH" if today().day % 2 == 1 else "ZH-EN"


def count_en_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", text))


def count_zh_chars(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


async def chat(messages: list[dict[str, str]], temperature: float = 0.4) -> str:
    if not API_KEY or API_KEY.startswith("sk-your"):
        raise HTTPException(500, "服务未配置，请检查本地设置。")
    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise HTTPException(502, f"服务请求失败：{r.status_code}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise HTTPException(502, "服务响应无法解析。") from exc


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    return json.loads(text)


def split_practice_chunks(text: str, direction: str) -> list[str]:
    """Split source material at natural punctuation without changing its wording."""
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return []
    is_chinese = direction == "ZH-EN"
    min_size, max_size = (100, 200) if is_chinese else (35, 85)
    units = [unit.strip() for unit in re.split(r"(?<=[。！？；!?;])\s*|\n+", cleaned) if unit.strip()]
    chunks: list[str] = []
    buffer = ""
    measure = count_zh_chars if is_chinese else count_en_words
    for unit in units:
        candidate = f"{buffer} {unit}".strip() if buffer else unit
        if measure(candidate) <= max_size:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        remaining = unit
        while measure(remaining) > max_size:
            marks = [match.end() for match in re.finditer(r"[，、,:：]", remaining)]
            cut = next((point for point in reversed(marks) if measure(remaining[:point]) <= max_size), 0)
            if not cut:
                cut = max_size if is_chinese else len(" ".join(remaining.split()[:max_size]))
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip(" ，、,:：")
        if remaining:
            buffer = remaining
    if buffer:
        if chunks and measure(buffer) < min_size and measure(f"{chunks[-1]} {buffer}") <= max_size:
            chunks[-1] = f"{chunks[-1]} {buffer}".strip()
        else:
            chunks.append(buffer)
    return chunks


def phrase_chunks(text: str, direction: str) -> list[str]:
    separators = r"([,;:，；：]|\b(?:and|but|while|because|although|when|if|that|which|who)\b)" if direction == "EN-ZH" else r"([，；：、]|因为|但是|不过|虽然|如果|当|并且|同时|因此)"
    parts = [part.strip(" ,;:，；：") for part in re.split(separators, text) if part and not re.fullmatch(separators, part)]
    return [part for part in parts if part][:5] or [text]


def slash_chunks(text: str, direction: str) -> str:
    return " / ".join(phrase_chunks(text, direction))


def recent_question_keys() -> set[str]:
    if not RECENT_QUESTION_STORE.exists():
        return set()
    try:
        data = json.loads(RECENT_QUESTION_STORE.read_text(encoding="utf-8"))
        if isinstance(data, list) and data and all(isinstance(item, list) for item in data):
            return {str(key) for batch in data[-6:] for key in batch}
        return {str(key) for key in data} if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def save_recent_question_batch(items: list[str]) -> None:
    RECENT_QUESTION_STORE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(RECENT_QUESTION_STORE.read_text(encoding="utf-8")) if RECENT_QUESTION_STORE.exists() else []
    except (json.JSONDecodeError, OSError):
        data = []
    batches = [batch for batch in data if isinstance(batch, list)] if isinstance(data, list) and data and all(isinstance(item, list) for item in data) else []
    batches.append(list(dict.fromkeys(items)))
    RECENT_QUESTION_STORE.write_text(json.dumps(batches[-6:], ensure_ascii=False, indent=2), encoding="utf-8")


def save_recent_question_keys(items: list[str]) -> None:
    save_recent_question_batch(items)


def question_key(text: str) -> str:
    return re.sub(r"[\W_]", "", str(text or ""), flags=re.UNICODE).lower()


def assert_unique_questions(questions: list[dict[str, Any]]) -> None:
    keys = [question_key(item.get("source", "")) for item in questions]
    if len(questions) != 5 or any(not key for key in keys) or len(set(keys)) != 5:
        raise HTTPException(502, "题目生成未通过唯一性校验，请重新开始练习。")


def unique_question_texts(items: list[str], direction: str, topic: str, fill_with_fallback: bool = True, exclude: set[str] | None = None) -> list[str]:
    def key(text: str) -> str:
        return question_key(text)

    unique: list[str] = []
    seen: set[str] = set(exclude or set())
    for item in items:
        text = str(item).strip()
        normalized = key(text)
        if text and normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(text)
    if fill_with_fallback:
        for fallback in fallback_questions(direction, topic):
            normalized = key(fallback)
            if normalized not in seen:
                seen.add(normalized)
                unique.append(fallback)
            if len(unique) == 5:
                break
    if len(unique) < 5:
        raise HTTPException(422, "当前材料不足 5 个不同片段，请补充更多内容。")
    return unique[:5]


def fallback_questions(direction: str, topic: str) -> list[str]:
    english = [
        "The city council announced a pilot programme to make public transport more accessible during peak hours, after commuters reported that crowded stations and irregular services made it difficult for them to reach work and school on time.",
        "Researchers say that clear rules, reliable data and regular teacher training are essential when new technologies are introduced into schools, because students need to understand both the benefits of the tools and the limits on their use.",
        "The company plans to reduce energy use by upgrading its older buildings over the next three years, installing more efficient heating systems and publishing annual progress reports so that local residents can follow the results.",
        "Local communities were invited to share their views before the final environmental proposal was published, with officials promising to explain how public comments would influence decisions on transport, housing and green spaces.",
        "Although the market remains uncertain, small businesses reported a cautious improvement in consumer demand, while warning that higher borrowing costs and unstable supply chains could still limit future investment.",
    ]
    chinese = [
        "市政府近日启动试点计划，重点改善高峰时段公共交通的便利性。有关部门表示，将根据客流变化调整班次，并在换乘站增加引导人员和实时信息提示，帮助通勤者更稳定地安排出行时间。",
        "研究人员指出，新技术进入校园时，清晰的规则和可靠的数据同样重要。学校不仅要说明工具可以怎样使用，也要帮助学生理解哪些信息需要核实、哪些作业环节必须独立完成。",
        "该公司计划在未来三年升级旧办公楼，以减少整体能源消耗。改造将包括更换供暖设备、优化照明系统和定期公开节能数据，使员工和周边社区能够了解实际进展。",
        "在最终环境方案公布前，当地社区受邀提交意见和建议。负责部门承诺说明公众反馈如何影响交通、住房和绿地安排，并在后续评估中继续公开相关依据。",
        "尽管市场仍存在不确定性，小微企业的消费需求已出现谨慎回升。不过企业也提醒，融资成本上升和供应链波动仍可能限制未来投资，因此需要根据订单变化及时调整经营计划。",
    ]
    scenarios_en = [
        "A regional hospital introduced an online appointment system after patients said that long waiting times and unclear instructions often delayed treatment, and managers will review the results every month before expanding the service.",
        "Farmers in several coastal towns are testing water-saving equipment because recent dry seasons have reduced crop yields, while local banks are offering small loans to help families pay for the initial investment.",
        "A public library extended its evening opening hours after students and shift workers asked for quieter study spaces, but staff said that the trial will continue only if enough volunteers can support weekend activities.",
        "The museum changed the way it presents historical objects by adding audio guides and short explanations for younger visitors, hoping that families can better understand how local traditions developed over time.",
        "Several neighbourhood shops agreed to share delivery vehicles in order to reduce traffic in the town centre, although they still need to decide how fuel costs and delivery schedules will be divided.",
        "University researchers found that regular tree planting can lower temperatures in crowded streets, but they stressed that young trees also require long-term watering and maintenance during the first few years.",
        "The national sports association revised its youth training plan after coaches reported that talented players often left the programme too early, and the new plan will provide more support for education and travel.",
        "A manufacturing company moved part of its supply chain closer to its main market after overseas delays affected production, yet executives said that the change would take time and might increase costs at first.",
        "Parents and teachers are discussing a new homework policy that would limit online assignments in the evening, because many families believe that younger students need more time for reading, exercise and sleep.",
        "The local government will publish a map of flood-prone streets before the rainy season, allowing residents to prepare emergency supplies and report blocked drains before heavy storms arrive.",
        "An independent cinema began showing more documentaries made by young directors, saying that the programme could attract audiences who want to discuss social issues after the film ends.",
        "Small exporters are seeking new customers in nearby countries after exchange-rate changes made long-distance sales less predictable, and trade advisers are offering workshops on contracts and insurance.",
        "The railway operator plans to replace older ticket machines with simpler touch-screen services, while keeping staffed counters at major stations for passengers who need additional assistance.",
        "Community health workers are visiting older residents who live alone, checking whether they have enough medicine and helping them contact local services when they need transport or daily support.",
        "A technology start-up delayed the release of its new payment service after security tests found several problems, explaining that customer trust would be more important than meeting the original launch date.",
    ]
    scenarios_zh = [
        "一家地区医院在患者反映候诊时间过长、就医指引不够清楚后，启用了线上预约系统。管理人员表示，将按月评估使用效果，再决定是否把服务扩大到更多科室。",
        "多个沿海城镇的农户正在试用节水设备，因为近年的干旱季节已经影响作物产量。当地银行同时提供小额贷款，帮助家庭承担前期投入。",
        "一座公共图书馆在学生和轮班工作者提出需求后延长了晚间开放时间，希望提供更安静的学习空间。不过工作人员表示，周末活动仍需要足够志愿者支持。",
        "博物馆通过增加语音导览和简短说明改变了历史展品的呈现方式，希望年轻观众和家庭能够更好理解当地传统是如何逐步形成的。",
        "多家社区商店同意共用配送车辆，以减少市中心的交通压力，但它们仍需要协商燃油费用和配送时间应该如何分配。",
        "大学研究人员发现，在拥挤街道持续种树可以降低局部温度，但他们强调，新树在最初几年仍需要长期浇水和维护。",
        "全国体育协会在教练反映有潜力的年轻运动员过早退出训练计划后，修订了培养方案，新计划将为教育安排和出行提供更多支持。",
        "一家制造企业在海外延误影响生产后，将部分供应链迁到更接近主要市场的地区，不过管理层表示，这项调整需要时间，初期成本也可能上升。",
        "家长和教师正在讨论新的作业政策，希望限制晚间线上作业，因为许多家庭认为低年级学生需要更多时间阅读、运动和休息。",
        "地方政府将在雨季前公布易积水街道地图，让居民提前准备应急物品，并在暴雨来临前报告被堵塞的排水设施。",
        "一家独立影院开始放映更多年轻导演制作的纪录片，认为这种安排能够吸引希望在影片结束后讨论社会议题的观众。",
        "汇率变化使远距离销售更难预测后，小型出口企业开始寻找周边国家的新客户，贸易顾问也在提供合同和保险方面的培训。",
        "铁路运营方计划用更简单的触屏服务替换旧售票机，同时在主要车站保留人工柜台，为需要额外帮助的乘客提供支持。",
        "社区健康工作者正在走访独居老人，确认他们是否有足够的药品，并在需要交通或日常帮助时协助联系当地服务。",
        "一家科技创业公司在安全测试发现多个问题后推迟了新支付服务的上线，解释说，获得用户信任比赶上原定发布日期更重要。",
    ]
    extra_en = [
        f"The {sector} launched a new {initiative} after residents asked for clearer information, and officials said they would measure the results before deciding whether to continue the programme next year."
        for sector, initiative in [
            ("city planning office", "consultation service"), ("public health agency", "prevention campaign"), ("regional university", "community research project"), ("local transport authority", "accessibility scheme"), ("environment department", "recycling programme"), ("small business network", "training course"), ("housing association", "repair reporting system"), ("cultural centre", "youth workshop"), ("emergency service", "safety information plan"), ("consumer group", "price comparison guide"), ("school district", "parent communication channel"), ("tourism board", "visitor management plan"), ("water authority", "conservation campaign"), ("employment office", "career support service"), ("community council", "neighbourhood meeting series")
        ]
    ]
    extra_zh = [
        f"{sector}在居民提出需要更清楚的信息后，启动了新的{initiative}。负责人表示，将先评估实际效果，再决定明年是否继续扩大这项安排。"
        for sector, initiative in [
            ("城市规划部门", "咨询服务"), ("公共卫生机构", "预防宣传计划"), ("地区大学", "社区研究项目"), ("交通管理部门", "无障碍服务方案"), ("环境部门", "回收利用计划"), ("小企业协会", "培训课程"), ("住房机构", "维修申报系统"), ("文化中心", "青年工作坊"), ("应急服务部门", "安全信息计划"), ("消费者组织", "价格比较指南"), ("学区管理部门", "家校沟通渠道"), ("旅游推广机构", "游客管理方案"), ("水务部门", "节水宣传活动"), ("就业服务机构", "职业支持服务"), ("社区委员会", "邻里议事安排")
        ]
    ]
    return (english + scenarios_en + extra_en) if direction == "EN-ZH" else (chinese + scenarios_zh + extra_zh)


async def harvest_headlines(direction: str, topic: str) -> list[dict[str, str]]:
    feeds = EN_FEEDS if direction == "EN-ZH" else ZH_FEEDS
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    async def fetch_feed(client: httpx.AsyncClient, outlet: str, url: str) -> list[dict[str, str]]:
        try:
            response = await client.get(url, headers={"User-Agent": "shiyi-coach/1.0"})
            parsed = feedparser.parse(response.content)
        except Exception:
            return []
        entries: list[dict[str, str]] = []
        for entry in parsed.entries[:12]:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc) if getattr(entry, "published_parsed", None) else None
            if published and published < cutoff:
                continue
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            entries.append({
                "outlet": outlet,
                "title": title,
                "summary": re.sub(r"<[^>]+>", "", entry.get("summary") or entry.get("description") or "")[:600],
                "url": entry.get("link") or "",
                "date": published.date().isoformat() if published else "",
            })
        return entries

    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0), follow_redirects=True) as client:
        groups = await asyncio.gather(*(fetch_feed(client, outlet, url) for outlet, url in feeds))
    items = [item for group in groups for item in group]
    topic_hint = TOPICS.get(topic, "")
    if topic not in ("AUTO", "") and topic_hint:
        ranked = [x for x in items if topic_hint[:2] in x["title"] or any(
            k in x["title"] + x["summary"]
            for k in {
                "POL": ["选举", "外交", "总统", "election", "diplomat", "president", "NATO", "UN"],
                "ECO": ["经济", "通胀", "利率", "GDP", "inflation", "rate", "market", "bank"],
                "TECH": ["科技", "芯片", "AI", "人工智能", "chip", "tech", "software"],
                "SOC": ["社会", "移民", "住房", "housing", "migration", "strike"],
                "ENV": ["气候", "碳", "气候", "climate", "carbon", "flood"],
                "EDU": ["教育", "卫生", "医院", "school", "health", "hospital"],
                "BIZ": ["公司", "并购", "财报", "company", "merger", "earnings"],
                "CUL": ["体育", "电影", "文化", "sport", "film", "olympic"],
            }.get(topic, [])
        )]
        if ranked:
            items = ranked + [x for x in items if x not in ranked]
    # unique by title
    seen: set[str] = set()
    uniq = []
    for it in items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)
    return uniq[:18]


async def make_ai_question_set(direction: str, topic: str, level: str) -> tuple[list[dict[str, Any]], str]:
    if not API_KEY or API_KEY.startswith("sk-your"):
        raise RuntimeError("练习服务暂不可用")
    headlines = await harvest_headlines(direction, topic)
    payload = {
        "task": "生成 5 道独立的短句视译练习题",
        "direction": direction,
        "topic": TOPICS.get(topic, TOPICS["AUTO"]),
        "level": level,
        "candidates": headlines[:8],
        "rules": [
            "每道题只给原句，不给译文或答案",
            "必须返回 5 道内容、主题、句式和训练重点都不同的题目，任何两题不得重复或仅做同义改写",
            "题目语言必须与 direction 的源语言一致：EN-ZH 为英文，ZH-EN 为中文",
            "每道中文题控制在约100-200个中文字符；英文题控制在约35-85个单词",
            "覆盖数字、逻辑、术语、长句拆分等不同训练点",
            "不得编造精确来源事实；若有候选，参考其主题改写为自洽训练句；若候选为空，围绕 topic 与 level 自行生成自洽训练句",
            "返回 JSON：{questions:[字符串,字符串,字符串,字符串,字符串]}",
        ],
    }
    raw = await chat(
        [
            {"role": "system", "content": "你是专业视译练习出题助手。只返回 JSON。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.65,
    )
    data = parse_json(raw)
    items = [str(item).strip() for item in data.get("questions", []) if str(item).strip()]
    if len(items) < 5:
        raise RuntimeError("模型未返回 5 道有效题目")
    normalized: list[str] = []
    for item in items:
        normalized.extend(split_practice_chunks(item, direction))
    unique = unique_question_texts(normalized, direction, topic, fill_with_fallback=True, exclude=recent_question_keys())
    return [
        {"id": f"q-{index + 1}", "order": index + 1, "source": item, "answer": "", "tone": "待标记", "rhythm": "待标记", "audio_meta": {}}
        for index, item in enumerate(unique)
    ], "系统练习题组"


def make_question_set(direction: str, topic: str, level: str, custom_text: str) -> tuple[list[dict[str, Any]], str]:
    direction = direction if direction in ("EN-ZH", "ZH-EN") else default_direction()
    topic = topic if topic in TOPICS else "AUTO"
    level = level if level in ("B2", "C1") else "C1"
    source = "备用练习题组"
    if custom_text.strip():
        raw_items = [part.strip() for part in re.split(r"\n+|(?<=[。！？.!?])\s*", custom_text) if part.strip()]
        sentences = raw_items if len(raw_items) >= 5 else split_practice_chunks(custom_text, direction)
        detected = "EN-ZH" if count_en_words(custom_text) >= count_zh_chars(custom_text) else "ZH-EN"
        if direction not in ("EN-ZH", "ZH-EN"):
            direction = detected
        source = "用户提供材料"
    else:
        sentences = fallback_questions(direction, topic)

    unique = unique_question_texts(sentences, direction, topic, fill_with_fallback=not custom_text.strip(), exclude=recent_question_keys() if not custom_text.strip() else set())
    questions = []
    for index, sentence in enumerate(unique):
        questions.append(
            {
                "id": f"q-{index + 1}",
                "order": index + 1,
                "source": sentence,
                "answer": "",
                "tone": "待标记",
                "rhythm": "待标记",
                "audio_meta": {},
            }
        )
    return questions, source


def score_band(score: int) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 60:
        return "合格"
    return "待提高"


def concrete_suggestion(answer: str, source: str, direction: str, coverage: float, pause_count: int) -> dict[str, str]:
    fragment = re.sub(r"\s+", " ", answer).strip()
    fragment = fragment[:48] if fragment else "（未识别到有效作答）"
    source_focus = re.sub(r"\s+", " ", source).strip()[:48]
    if coverage < 0.65:
        diagnosis = "作答长度明显短于原句，核心信息可能被压缩，容易遗漏主干、逻辑关系或限定成分。"
        action = "先圈出原句的主语、动作和结果，再按“背景/条件→主干→补充信息”的顺序补全；每补一句都回看原句核对是否遗漏。"
        revised = f"先补出“{source_focus}”中的主干，再补入限制或结果信息。"
        reason = "先保住信息主干能降低漏译，补充成分放在后面更容易保持表达完整。"
    elif pause_count >= 3:
        diagnosis = "表达中出现多次较长停顿，说明组织语言时可能在句中临时找词，导致节奏被切碎。"
        action = "把作答预先分成两到三个意群；每个意群内部连续说完，只在逻辑转换处短停，不在主语、谓语或固定搭配中间断开。"
        revised = f"“{fragment}” → 先连续说出主干，再在逻辑转换处短停后补充细节。"
        reason = "意群内不断开能让听者先获得完整主干，停顿也更符合视译的自然节奏。"
    else:
        diagnosis = "作答已有基本信息，但表达仍可通过更明确的连接和主干顺序提升清晰度。"
        action = "保留原有信息，先用一个明确主语引出动作，再补结果；检查相邻两个分句是否有因果、转折或递进关系，并补上对应连接词。"
        revised = f"“{fragment}” → “先交代谁/什么做了什么，再用连接词补出结果或转折”。"
        reason = "主干先行、连接关系明确，能减少跳跃感，也便于在有限时间内稳定输出。"
    return {"source_quote": fragment, "diagnosis": diagnosis, "action": action, "example": revised, "reason": reason}


def sentence_position(answer: str) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s*", answer) if part.strip()]
    return f"第 {len(sentences) or 1} 句"


def repeated_token(text: str) -> str:
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|\b[A-Za-z]{3,}\b", text):
        if re.search(r"[A-Za-z]", token):
            count = len(re.findall(rf"\b{re.escape(token)}\b", text, re.I))
        else:
            count = len(re.findall(re.escape(token), text))
        if count >= 2:
            return token
    return ""


def dynamic_scores(answer: str, direction: str) -> dict[str, dict[str, Any]]:
    text = answer.strip()
    units = count_zh_chars(text) if direction == "EN-ZH" else count_en_words(text)
    repeated = repeated_token(text)
    punctuation_penalty = 8 if len(re.findall(r"[，。；！？,.!?;]", text)) == 0 and units > 18 else 0
    grammar_penalty = 10 if not re.search(r"[。！？.!?]$", text) else 0
    logic_penalty = 8 if units > 20 and not re.search(r"因此|但是|不过|同时|because|however|therefore|while|although", text, re.I) else 0
    variety_penalty = 7 if repeated else 0
    natural_penalty = min(15, max(0, 12 - units // 8))
    return {
        "grammar_accuracy": {"score": max(45, 92 - grammar_penalty - punctuation_penalty), "deduction": "缺少句末标点" if grammar_penalty else "未发现明显句末结构问题"},
        "word_precision": {"score": max(45, 90 - variety_penalty), "deduction": f"“{repeated}”重复出现，建议替换其中一次" if repeated else "未发现重复实词"},
        "sentence_variety": {"score": max(45, 88 - variety_penalty - punctuation_penalty // 2), "deduction": "句内层次不足" if punctuation_penalty else "句式层次基本完整"},
        "logic_coherence": {"score": max(45, 90 - logic_penalty), "deduction": "长句缺少明确连接词" if logic_penalty else "逻辑关系有基本承接"},
        "spoken_naturalness": {"score": max(45, 90 - natural_penalty), "deduction": "作答过短，口语信息不完整" if natural_penalty else "信息长度足以形成自然表达"},
    }


def normalize_sentence_end(text: str, direction: str) -> str:
    body = re.sub(r"[。！？.!?]+\s*$", "", text.strip())
    return f"{body}{'。' if direction == 'EN-ZH' else '.'}" if body else ""


def sentence_feedback(question: dict[str, Any], direction: str, order: int) -> dict[str, Any]:
    source = str(question.get("source") or "").strip()
    answer = str(question.get("answer") or "").strip()
    audio = question.get("audio_meta") or {}
    pauses = [item for item in audio.get("pauses") or [] if isinstance(item, dict)]
    answer_position = f"第 {order} 句"
    source_breaks = slash_chunks(source, direction)
    answer_breaks = slash_chunks(answer, direction)
    answer_parts = phrase_chunks(re.sub(r"[。！？.!?]+\s*$", "", answer), direction)
    first_part = answer_parts[0] if answer_parts else answer
    scores = dynamic_scores(answer, direction)
    repeated = repeated_token(answer)
    source_numbers = re.findall(r"\b\d+(?:[.,]\d+)?%?\b", source)
    missing_number = next((number for number in source_numbers if number not in answer), "")
    has_terminal = bool(re.search(r"[。！？.!?]\s*$", answer))
    connector_pattern = r"因此|但是|不过|同时|由于|因为|although|because|however|therefore|while|but"
    missing_connector = len(answer_parts) >= 2 and not re.search(connector_pattern, answer, re.I)
    output_is_chinese = direction == "EN-ZH"
    joiner = "，因此" if output_is_chinese else ", therefore "
    if repeated:
        diagnosis = f"问题类型：用词重复。你在“{answer}”中连续使用“{repeated}”，但两处承担的信息功能不同，第二处没有补充新的动作或结果，导致句子原地重复。"
        revised_body = re.sub(re.escape(repeated), "该项", answer, count=1, flags=re.I)
        revised = normalize_sentence_end(revised_body, direction)
        changes = f"1. 保留后一次“{repeated}”作为核心信息。\n2. 将前一次“{repeated}”改为“{'该项' if output_is_chinese else 'this measure'}”，避免同一实词连续重复。\n3. 统一句末为一个标点。"
        reason = f"“{repeated}”只保留一次后，前半句用于指代、后半句承载关键信息，听者能区分两层功能；不改会让人误以为你在重复同一信息。"
    elif missing_number:
        diagnosis = f"问题类型：关键信息遗漏。原句包含“{missing_number}”，但你的作答“{answer}”没有保留这个数字，数字对应的时间、比例或数量无法被听者确认。"
        revised = normalize_sentence_end(f"{answer_parts[0] if answer_parts else answer}{'，并补出' if output_is_chinese else ', and include '}{missing_number}", direction)
        changes = f"1. 在“{first_part}”后的信息位补入“{missing_number}”。\n2. 先说数字，再说它修饰的对象。\n3. 统一句末为一个标点。"
        reason = f"补出“{missing_number}”后，数量信息与对象重新对应，听者能判断事实范围；不补会把原句中的关键限制条件丢失。"
    elif not has_terminal:
        diagnosis = f"问题类型：句末边界缺失。你的作答“{answer}”在结尾没有句末标点或明确收束，前一句与下一条信息容易连成一串，听者无法判断本句何时结束。"
        revised = normalize_sentence_end(answer, direction)
        changes = "1. 保留原句全部用词。\n2. 仅在句尾补一个句末标点。\n3. 录音时在句末完成收束后再进入下一句。"
        reason = f"“{answer}”加上句末边界后，听者能明确接收本句结束；不加会让下一句像是前一句的附属成分，影响信息分层。"
    elif missing_connector:
        diagnosis = f"问题类型：逻辑连接缺失。你在“{first_part}”之后直接接入下一意群，但没有说明两部分是因果、转折还是递进，关系只能让听者猜测。"
        revised = normalize_sentence_end(joiner.join(answer_parts), direction)
        changes = f"1. 保留“{first_part}”作为第一意群。\n2. 在第二意群前加入“{'因此' if output_is_chinese else 'therefore'}”，明确关系。\n3. 合并多余句末标点，只保留一个结束标点。"
        reason = f"加入连接词后，前一意群成为原因或依据，后一意群成为结果；不加时，听者可能把两部分误解为并列信息。"
    else:
        diagnosis = f"问题类型：信息顺序。你的作答以“{first_part}”开头，但主干与补充信息没有通过停顿或标点分层，重要动作和附加说明同时涌入，重点不够突出。"
        revised = normalize_sentence_end("，".join(answer_parts), direction) if output_is_chinese else normalize_sentence_end(", ".join(answer_parts), direction)
        changes = f"1. 保留“{first_part}”作为主干起点。\n2. 在每个意群边界加入停顿或逗号。\n3. 删除重复句末标点，只保留一个结束标点。"
        reason = f"改后先让“{first_part}”承担主干，再逐层补充其余信息，听者能先抓住核心动作；不分层会使补充信息抢占主干位置。"
    pause_note = "本句未采集到可用停顿片段；请只在“切分后”的斜杠位置短停，避免在主谓或术语中间换气。"
    if pauses:
        first = pauses[0]
        second = round(float(first.get("start_ms", 0)) / 1000, 1)
        duration = round(float(first.get("duration_ms", 0)) / 1000, 1)
        pause_note = f"本句在第 {second} 秒处停顿约 {duration} 秒；请将该停顿对照“{answer_breaks}”检查，若不在斜杠边界，缩短到 0.3 秒以内。"
    source_units = count_en_words(source) if direction == "EN-ZH" else count_zh_chars(source)
    answer_units = count_zh_chars(answer) if direction == "EN-ZH" else count_en_words(answer)
    coverage = min(1, answer_units / max(1, source_units * (1.15 if direction == "EN-ZH" else 0.45)))
    overall = round(sum(item["score"] for item in scores.values()) / len(scores) * 0.55 + coverage * 45)
    source_parts = phrase_chunks(re.sub(r"[。！？.!?]+\s*$", "", source), direction)
    source_main = source_parts[0] if source_parts else source
    source_support = " / ".join(source_parts[1:]) if len(source_parts) > 1 else "原句没有拆出第二个独立补充意群"
    teaching_steps = [
        {"title": "先抓原句主干", "content": f"原句的第一意群是“{source_main}”。练习时先说清其中的主体、动作和对象，不要一开始就插入后面的背景或结果。"},
        {"title": "再核对你的对应表达", "content": f"你的作答从“{first_part}”开始。请逐词核对：它是否已经覆盖原句主干“{source_main}”；若没有，先补主干，再处理附加信息。"},
        {"title": "补充信息放在主干之后", "content": f"原句后续还有“{source_support}”。这部分承担时间、原因、条件、结果或限制信息；应在主干完成后，用一个明确连接词或短停再补入。"},
        {"title": "按改后句复述", "content": f"先完整读“{revised}”。第一遍只确保主干完整，第二遍再把补充信息说完；不要为了追求速度删掉数字、条件或结果。"},
        {"title": "复述后的自检", "content": f"请回看“{answer}”：听者能否立刻回答‘谁/什么做了什么’、‘为什么或在什么条件下’、‘结果是什么’？其中任一项答不出来，就回到对应意群补说。"},
    ]
    return {
        "score": max(0, min(100, overall)), "label": score_band(max(0, min(100, overall))),
        "source_quote": f"{answer_position}｜“{answer}”",
        "breakdown": {"before": source, "after": source_breaks, "answer_before": answer, "answer_after": answer_breaks},
        "diagnosis": diagnosis,
        "example": {"original": answer, "revised": revised, "changes": changes},
        "reason": reason,
        "teaching": teaching_steps,
        "structure": {"main_clause": f"主干：{first_part}", "supporting_information": f"补充信息：{''.join(answer_parts[1:]) if len(answer_parts) > 1 else '本句未形成独立补充意群'}", "evidence": f"你的作答原句：“{answer}”"},        "manual_mark": {"tone": question.get("tone", "待标记"), "rhythm": question.get("rhythm", "待标记"), "scores": scores},
        "pause_rhythm": pause_note,
        "wording": {"score": scores["word_precision"]["score"], "issue": scores["word_precision"]["deduction"], "sentence_note": scores["sentence_variety"]["deduction"]},
        "content_accuracy": {"score": round(coverage * 100), "comment": "已按作答长度和信息单元估算覆盖度。"},
        "expression_fluency": {"score": scores["spoken_naturalness"]["score"], "comment": scores["spoken_naturalness"]["deduction"]},
        "tone": {"score": scores["spoken_naturalness"]["score"], "comment": f"手动标记：{question.get('tone', '待标记')}。"},
        "rhythm": {"score": scores["logic_coherence"]["score"], "comment": pause_note},
        "wording_structure": {"score": scores["word_precision"]["score"], "comment": scores["word_precision"]["deduction"]},
        "suggestion": {"source_quote": f"{answer_position}｜“{answer}”", "diagnosis": diagnosis, "action": changes, "example": f"原句：{answer}\n改后句：{revised}", "reason": reason},
    }


def rule_question_feedback(question: dict[str, Any], direction: str, order: int = 1) -> dict[str, Any]:
    return sentence_feedback(question, direction, order)


DIMENSION_KEYS = ("grammar_accuracy", "word_precision", "sentence_variety", "logic_coherence", "spoken_naturalness")
METRIC_KEYS = ("content_accuracy", "expression_fluency", "tone", "rhythm", "wording_structure")


def score_value(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return max(0, min(100, int(default)))


def text_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def merge_ai_feedback(baseline: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Overlay AI-written feedback onto the rule baseline so every frontend field stays present."""
    if not isinstance(data, dict):
        return baseline
    result = baseline
    for key in ("diagnosis", "reason", "pause_rhythm"):
        text = text_value(data.get(key))
        if text:
            result[key] = text
    structure = data.get("structure")
    if isinstance(structure, dict):
        for key in ("main_clause", "supporting_information", "evidence"):
            text = text_value(structure.get(key))
            if text:
                result["structure"][key] = text
    example = data.get("example")
    if isinstance(example, dict):
        for key in ("revised", "changes"):
            text = text_value(example.get(key))
            if text:
                result["example"][key] = text
    teaching = data.get("teaching")
    if isinstance(teaching, list):
        steps = [
            {"title": text_value(item.get("title")), "content": text_value(item.get("content"))}
            for item in teaching
            if isinstance(item, dict) and text_value(item.get("title")) and text_value(item.get("content"))
        ]
        if len(steps) >= 3:
            result["teaching"] = steps[:6]
    wording = data.get("wording")
    if isinstance(wording, dict):
        for key in ("issue", "sentence_note"):
            text = text_value(wording.get(key))
            if text:
                result["wording"][key] = text
    scores = data.get("scores")
    if isinstance(scores, dict):
        for key in DIMENSION_KEYS:
            item = scores.get(key)
            if isinstance(item, dict):
                value = score_value(item.get("score"), result["manual_mark"]["scores"][key]["score"])
                comment = text_value(item.get("comment"))
            else:
                value = score_value(item, result["manual_mark"]["scores"][key]["score"])
                comment = ""
            result["manual_mark"]["scores"][key]["score"] = value
            if comment:
                result["manual_mark"]["scores"][key]["deduction"] = comment
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        for key in METRIC_KEYS:
            item = metrics.get(key)
            if isinstance(item, dict):
                result[key] = {
                    "score": score_value(item.get("score"), result.get(key, {}).get("score", 0)),
                    "comment": text_value(item.get("comment")) or result.get(key, {}).get("comment", ""),
                }
    dim_values = [item["score"] for item in result["manual_mark"]["scores"].values()]
    overall = score_value(data.get("score"), round(sum(dim_values) / len(dim_values)))
    result["score"] = overall
    result["label"] = score_band(overall)
    result["wording"]["score"] = result["manual_mark"]["scores"]["word_precision"]["score"]
    result["suggestion"] = {
        "source_quote": result["source_quote"],
        "diagnosis": result["diagnosis"],
        "action": result["example"]["changes"],
        "example": f"原句：{result['example']['original']}\n改后句：{result['example']['revised']}",
        "reason": result["reason"],
    }
    return result


async def ai_sentence_feedback(question: dict[str, Any], direction: str, order: int) -> dict[str, Any]:
    baseline = sentence_feedback(question, direction, order)
    if not API_KEY or API_KEY.startswith("sk-your"):
        return baseline
    payload = {
        "task": "对一条视译作答做逐句深度点评，严格按给定模板返回 JSON",
        "direction": direction,
        "direction_note": "EN-ZH 为英译中；ZH-EN 为中译英",
        "order": order,
        "source": str(question.get("source") or "").strip(),
        "answer": str(question.get("answer") or "").strip(),
        "manual_tone": question.get("tone", "待标记"),
        "manual_rhythm": question.get("rhythm", "待标记"),
        "audio_meta": question.get("audio_meta") or {},
        "rule_baseline": {
            "diagnosis": baseline["diagnosis"],
            "source_breakdown": baseline["breakdown"]["after"],
            "answer_breakdown": baseline["breakdown"]["answer_after"],
            "revised_example": baseline["example"]["revised"],
            "changes": baseline["example"]["changes"],
            "reason": baseline["reason"],
            "pause_rhythm": baseline["pause_rhythm"],
            "scores": {key: item["score"] for key, item in baseline["manual_mark"]["scores"].items()},
        },
        "template": {
            "diagnosis": "问题类型 + 原文证据 + 作答证据 + 对听者的具体影响；必须引用具体词句，禁止空泛评价",
            "structure": {"main_clause": "主干", "supporting_information": "补充信息", "evidence": "逐词对照证据"},
            "example": {"revised": "目标语改后句：EN-ZH 输出中文，ZH-EN 输出英文且语法正确", "changes": "1. ...\n2. ...\n3. ..."},
            "reason": "说明改动如何提升信息完整性、传递流畅度或目标语言质量，并对应 Han (2015)",
            "teaching": [{"title": "步骤名", "content": "可执行的练习动作"}],
            "pause_rhythm": "结合停顿数据给出可执行建议",
            "wording": {"issue": "用词或术语问题", "sentence_note": "句式与信息分层问题"},
            "scores": {
                "grammar_accuracy": {"score": 0, "comment": "语法准确性依据"},
                "word_precision": {"score": 0, "comment": "用词精准度依据"},
                "sentence_variety": {"score": 0, "comment": "句式多样性依据"},
                "logic_coherence": {"score": 0, "comment": "逻辑连贯度依据"},
                "spoken_naturalness": {"score": 0, "comment": "口语自然度依据"},
            },
            "metrics": {
                "content_accuracy": {"score": 0, "comment": "信息完整性依据"},
                "expression_fluency": {"score": 0, "comment": "传递流畅度依据"},
                "tone": {"score": 0, "comment": "语气判断"},
                "rhythm": {"score": 0, "comment": "节奏判断"},
                "wording_structure": {"score": 0, "comment": "目标语言质量依据"},
            },
            "score": 0,
        },
        "rules": [
            "逐词对照原文与作答，先给证据再定档；不得编造原文没有的事实、数字或来源。",
            "同一错误只扣一个维度：数字、专名、逻辑关系错误归信息完整性；卡顿、重复、停顿归传递流畅度；语法、搭配、术语归目标语言质量。",
            "参考 rule_baseline 的诊断方向，但必须纠正、扩展和深化，不能照抄。",
            "teaching 至少 5 步，每步给出可执行动作。",
            "scores 与 metrics 均为 0-100 的整数，且必须与诊断一致。",
            "只返回 JSON，不要 Markdown 代码块。",
        ],
    }
    try:
        raw = await chat(
            [
                {"role": "system", "content": PROMPT + "\n你是资深视译考官与口译教练。严格按 Han (2015) 量表做证据化点评，只返回 JSON。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.3,
        )
        return merge_ai_feedback(baseline, parse_json(raw))
    except Exception:
        return baseline


async def review_practice_questions(req: PracticeReviewRequest) -> dict[str, Any]:
    questions = req.questions[:5]
    if len(questions) != 5:
        raise HTTPException(422, "请完成一组 5 道题后再提交统一点评。")
    for question in questions:
        if not str(question.get("source") or "").strip() or not str(question.get("answer") or "").strip():
            raise HTTPException(422, "请先完成全部 5 道题的作答。")

    async def build(index: int, question: dict[str, Any]) -> dict[str, Any]:
        try:
            feedback = await asyncio.wait_for(ai_sentence_feedback(question, req.direction, index + 1), timeout=75)
        except (Exception, asyncio.TimeoutError):
            feedback = sentence_feedback(question, req.direction, index + 1)
        return {"id": question.get("id"), "feedback": feedback}

    reviews = await asyncio.gather(*(build(index, question) for index, question in enumerate(questions)))
    average = round(sum(item["feedback"]["score"] for item in reviews) / len(reviews))
    return {
        "mode": "practice_review",
        "session_key": "",
        "overall_score": average,
        "overall_label": score_band(average),
        "summary": "已完成 AI 逐句点评。每句都按“原文定位—问题诊断—具体改法—原因说明—复述步骤”给出证据化分析，请逐项练习。",
        "questions": reviews,
    }


def load_practice_records() -> list[dict[str, Any]]:
    if not PRACTICE_STORE.exists():
        return []
    try:
        data = json.loads(PRACTICE_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_practice_records(records: list[dict[str, Any]]) -> None:
    PRACTICE_STORE.parent.mkdir(parents=True, exist_ok=True)
    PRACTICE_STORE.write_text(json.dumps(records[:20], ensure_ascii=False, indent=2), encoding="utf-8")


def record_preview(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "created_at": record["created_at"],
        "direction": record["direction"],
        "source": record.get("source", ""),
        "overall_score": record.get("review", {}).get("overall_score", 0),
        "overall_label": record.get("review", {}).get("overall_label", "待提高"),
        "question_count": len(record.get("questions", [])),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    d = default_direction()
    return {
        "today": today().isoformat(),
        "default_direction": d,
        "default_direction_label": "英译中" if d == "EN-ZH" else "中译英",
        "topics": TOPICS,
        "has_key": bool(API_KEY and not API_KEY.startswith("sk-your")),
        "asr_ready": bool(DASHSCOPE_API_KEY and not DASHSCOPE_API_KEY.startswith("sk-your")),
    }


@app.post("/api/practice/transcribe")
async def practice_transcribe(direction: str = "EN-ZH", audio: UploadFile = File(...)) -> dict[str, Any]:
    return await transcribe_audio(await audio.read(), audio.content_type or "audio/webm", direction)


@app.post("/api/practice/start")
async def practice_start(req: PracticeStartRequest) -> dict[str, Any]:
    direction = req.direction if req.direction in ("EN-ZH", "ZH-EN") else default_direction()
    topic = req.topic if req.topic in TOPICS else "AUTO"
    level = req.level if req.level in ("B2", "C1") else "C1"
    if req.custom_text.strip():
        questions, source = make_question_set(direction, topic, level, req.custom_text)
    else:
        try:
            questions, source = await asyncio.wait_for(make_ai_question_set(direction, topic, level), timeout=25)
        except (Exception, asyncio.TimeoutError):
            questions, source = make_question_set(direction, topic, level, "")
    assert_unique_questions(questions)
    save_recent_question_batch([question_key(question.get("source")) for question in questions])
    return {
        "direction": direction,
        "topic": topic,
        "level": level,
        "source": source,
        "question_count": len(questions),
        "questions": questions,
        "message": "请逐题作答。进入下一题后仍可回看和修改，五题完成后统一点评。",
    }


@app.post("/api/practice/review")
async def practice_review(req: PracticeReviewRequest) -> dict[str, Any]:
    return await review_practice_questions(req)


def apply_manual_scores(feedback: dict[str, Any]) -> dict[str, Any]:
    scores = feedback.get("manual_mark", {}).get("scores") or {}
    values = [max(0, min(100, int(item.get("score", 0)))) for item in scores.values() if isinstance(item, dict)]
    if values:
        feedback["score"] = round(sum(values) / len(values))
        feedback["label"] = score_band(feedback["score"])
        mapping = {
            "grammar_accuracy": "content_accuracy",
            "word_precision": "wording_structure",
            "sentence_variety": "wording_structure",
            "logic_coherence": "rhythm",
            "spoken_naturalness": "expression_fluency",
        }
        for key, target in mapping.items():
            if isinstance(scores.get(key), dict):
                feedback.setdefault(target, {})["score"] = int(scores[key].get("score", 0))
                feedback[target]["comment"] = scores[key].get("deduction", "")
    return feedback


def review_total(review: dict[str, Any]) -> tuple[int, str]:
    items = [apply_manual_scores(item.get("feedback", {})) for item in review.get("questions", [])]
    values = [int(item.get("score", 0)) for item in items]
    total = round(sum(values) / len(values)) if values else 0
    return total, score_band(total)


@app.post("/api/practice/save")
def practice_save(req: PracticeSaveRequest) -> dict[str, Any]:
    if len(req.questions) != 5 or any(not str(item.get("answer") or "").strip() for item in req.questions):
        raise HTTPException(422, "请完成 5 道题后再保存练习记录。")
    if not req.review.get("questions") or len(req.review["questions"]) != 5:
        raise HTTPException(422, "请先完成统一批改后再保存。")
    req.review["overall_score"], req.review["overall_label"] = review_total(req.review)
    for item in req.review.get("questions", []):
        apply_manual_scores(item.get("feedback", {}))
    record = {
        "id": uuid4().hex,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "direction": req.direction,
        "topic": req.topic,
        "level": req.level,
        "source": req.source,
        "questions": req.questions,
        "review": req.review,
        "session_key": str(req.review.get("session_key") or ""),
    }
    records = load_practice_records()
    if record["session_key"] and any(item.get("session_key") == record["session_key"] for item in records):
        raise HTTPException(409, "这组练习已保存，请到练习记录中查看。")
    records.insert(0, record)
    save_practice_records(records)
    return {"record": record_preview(record), "message": "练习记录已保存。"}


@app.get("/api/practice/records")
def practice_records() -> dict[str, Any]:
    records = load_practice_records()
    return {"records": [record_preview(record) for record in records[:20]], "total": len(records[:20])}


def han_band(score: float) -> dict[str, Any]:
    value = max(0, min(100, round(score)))
    if value >= 90:
        return {"score": 8, "band": 4, "label": "第4档（7-8分）", "descriptor": "达到量表第4档：关键信息、传递过程或目标语表达整体稳定，只存在少量可定位问题。"}
    if value >= 75:
        return {"score": 6, "band": 3, "label": "第3档（5-6分）", "descriptor": "达到量表第3档：整体可接受，但若干处遗漏、不流畅或表达问题会持续影响专业完成度。"}
    if value >= 55:
        return {"score": 4, "band": 2, "label": "第2档（3-4分）", "descriptor": "达到量表第2档：问题已形成规律，听者需要反复补足信息或重新组织句子才能理解。"}
    return {"score": 2, "band": 1, "label": "第1档（1-2分）", "descriptor": "达到量表第1档：问题频繁出现，已经可能阻断命题内容、传递过程或目标语理解。"}


def review_evidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for record_index, record in enumerate(records, 1):
        feedbacks = {item.get("id"): item.get("feedback", {}) for item in record.get("review", {}).get("questions", [])}
        for question_index, question in enumerate(record.get("questions", []), 1):
            feedback = feedbacks.get(question.get("id"), {})
            answer = str(question.get("answer") or "").strip()
            source = str(question.get("source") or "").strip()
            if not answer or not source:
                continue
            evidence.append({
                "index": f"练习{record_index}-题{question_index}",
                "created_at": record.get("created_at", ""),
                "source": source,
                "answer": answer,
                "diagnosis": feedback.get("diagnosis") or feedback.get("suggestion", {}).get("diagnosis", ""),
                "revised": feedback.get("example", {}).get("revised") or feedback.get("suggestion", {}).get("example", ""),
                "reason": feedback.get("reason") or feedback.get("suggestion", {}).get("reason", ""),
                "pause": feedback.get("pause_rhythm", ""),
                "scores": {key: feedback.get(key, {}).get("score") for key in ("content_accuracy", "expression_fluency", "tone", "rhythm", "wording_structure")},
            })
    return evidence


def dimension_report(name: str, scale_name: str, evidence: list[dict[str, Any]], score_keys: tuple[str, ...]) -> dict[str, Any]:
    values = [item["scores"][key] for item in evidence for key in score_keys if isinstance(item["scores"].get(key), (int, float))]
    average = round(sum(values) / len(values)) if values else 0
    band = han_band(average)
    samples = evidence[:3]
    cases = []
    for sample in samples:
        cases.append({
            "index": sample["index"],
            "original_transcript": sample["answer"],
            "source": sample["source"],
            "problem": sample["diagnosis"] or f"该句在{scale_name}上的扣分集中在“{sample['answer'][:36]}”这一片段。",
            "reference": sample["revised"] or sample["source"],
            "reason": sample["reason"] or sample["pause"] or "该处需要把命题内容、停顿位置和目标语搭配重新对齐。",
        })
    case_text = "".join(f"证据{index + 1}（{item['index']}）：原音转写是“{item['original_transcript']}”；原文命题是“{item['source']}”；问题定位在“{item['problem']}”；标准改法为“{item['reference']}”；原因是“{item['reason']}”。" for index, item in enumerate(cases))
    diagnosis = (
        f"{name}本次按 Han (2015) 的{scale_name}定档，样本均分为 {average}/100，对应{band['label']}。{band['descriptor']}"
        f"定档不是根据笼统印象，而是逐条回看已保存练习中的原句、用户转写和逐句反馈。{case_text}"
        f"扣分现象需要分开看：第一，用户实际说出的命题是否覆盖原文的主体、动作、对象、条件、数量和结果；第二，转写中的停顿、重复和句界是否打断了这些命题的接收顺序；第三，目标语搭配、术语和句末收束是否让听者无需猜测。"
        f"如果只说“信息完整”或“表达流畅”，无法说明分数来自哪里，因此这里把每一条证据都保留了原音转写和标准改法。听者真正接收到的是转写文本所呈现的顺序：先听到“{samples[0]['answer'][:42] if samples else '用户作答'}”，再试图回推原文“{samples[0]['source'][:42] if samples else '原句'}”。当两者的主干、补充信息或术语不能逐项对应时，分数就必须下移到当前档次，而不是因为单次整体印象给予更高档。"
        f"改进时不能把三个维度混成一次泛泛练习。{name}只处理{scale_name}：先标出原句命题，再听自己的转写，最后只修正本维度导致失分的词、停顿或搭配。同一处数字、专名或逻辑错误只计入信息完整性；同一处卡顿、重复和气口只计入传递流畅度；同一处语法、搭配和术语问题只计入目标语言质量，避免重复扣分。"
    )
    return {"name": name, "scale": scale_name, "average": average, "han": band, "diagnosis": diagnosis, "cases": cases}


def build_focus_plan(weakest: dict[str, Any]) -> dict[str, Any]:
    plans = {
        "信息完整性": ("选择包含数字、条件、因果和结果的150—220词新闻分析", "第一步圈出每句的主体、动作、对象、条件、数量和结果；第二步遮住原文只看标记；第三步录音后逐项核对转写", "完成5个长难句，每句复述3遍；每遍必须保留全部数字和条件，漏1项即重做"),
        "传递流畅度": ("选择已经理解内容、但句子含两个以上意群的材料", "第一步按意群画斜线；第二步每个意群连续说完；第三步只在斜线处停顿并回听转写", "完成5个句子、每句3遍；单次停顿不超过1.5秒，实词重音至少落在主语、动词和结果上"),
        "目标语言质量": ("选择含术语、固定搭配和逻辑连接的段落", "第一步先写术语对照；第二步只改搭配和连接词；第三步朗读改后句并检查是否像目标语", "整理10个术语和5个长句，每个长句做3个合格改写，错误搭配为0后再进入录音"),
    }
    material, steps, metric = plans.get(weakest["name"], plans["目标语言质量"])
    return {"name": weakest["name"], "days": 7, "material": material, "steps": steps, "metric": metric, "advice": f"未来7天只针对{weakest['name']}。{material}。{steps}。量化标准：{metric}。"}


def merge_ai_review(base: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return base
    for key in ("conclusion", "evolution"):
        text = text_value(data.get(key))
        if text:
            base[key] = text
    plan = data.get("focus_plan")
    if isinstance(plan, dict):
        for key in ("name", "material", "steps", "metric", "advice"):
            text = text_value(plan.get(key))
            if text:
                base["focus_plan"][key] = text
        days = plan.get("days")
        if isinstance(days, (int, float)):
            base["focus_plan"]["days"] = max(1, min(30, int(days)))
    weaknesses = data.get("weaknesses")
    if isinstance(weaknesses, list):
        by_name = {item.get("name"): item for item in base.get("weaknesses", [])}
        for item in weaknesses:
            if not isinstance(item, dict):
                continue
            target = by_name.get(text_value(item.get("name")))
            if not target:
                continue
            diagnosis = text_value(item.get("diagnosis"))
            if diagnosis:
                target["diagnosis"] = diagnosis
            cases = item.get("cases")
            if isinstance(cases, list):
                cleaned = [
                    {
                        "index": text_value(case.get("index")),
                        "original_transcript": text_value(case.get("original_transcript")),
                        "source": text_value(case.get("source")),
                        "problem": text_value(case.get("problem")),
                        "reference": text_value(case.get("reference")),
                        "reason": text_value(case.get("reason")),
                    }
                    for case in cases
                    if isinstance(case, dict) and text_value(case.get("index"))
                ]
                if cleaned:
                    target["cases"] = cleaned[:4]
    errors = data.get("top_errors")
    if isinstance(errors, list):
        cleaned_errors = [
            {
                "index": text_value(item.get("index")),
                "source": text_value(item.get("source")),
                "answer": text_value(item.get("answer")),
                "problem": text_value(item.get("problem")),
            }
            for item in errors
            if isinstance(item, dict) and text_value(item.get("index"))
        ]
        if cleaned_errors:
            base["top_errors"] = cleaned_errors[:5]
    return base


async def ai_professional_review(
    records: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    overall: int,
    previous_weakness: str,
    base_result: dict[str, Any],
) -> dict[str, Any]:
    if not API_KEY or API_KEY.startswith("sk-your"):
        return base_result
    compact = [
        {
            "index": item["index"],
            "source": item["source"][:220],
            "answer": item["answer"][:220],
            "diagnosis": item["diagnosis"][:180],
            "revised": item["revised"][:180],
            "reason": item["reason"][:180],
            "pause": item["pause"][:140],
            "scores": item["scores"],
        }
        for item in evidence[:12]
    ]
    payload = {
        "task": "基于多次已保存练习生成专业复盘，严格按给定模板返回 JSON",
        "framework": "Han (2015)：信息完整性、传递流畅度、目标语言质量；每个维度先列证据再定档。",
        "stats": {
            "count": len(records),
            "overall_score": overall,
            "score_trend": base_result["score_trend"],
            "previous_weakness": previous_weakness,
            "dimensions": [{"name": item["name"], "average": item["average"], "han": item["han"]} for item in dimensions],
        },
        "evidence": compact,
        "template": {
            "conclusion": "总判断：三维度分别处于哪一档、最低维度是什么、与上次复盘相比是否转移",
            "evolution": "上次首要弱点与本次首要弱点的对比，以及下一步只练什么",
            "weaknesses": [
                {
                    "name": "信息完整性 / 传递流畅度 / 目标语言质量（必须原样使用）",
                    "diagnosis": "先引用证据，再说明扣分机制，最后给出该维度的训练动作",
                    "cases": [{"index": "证据编号", "original_transcript": "原音转写", "source": "原文命题", "problem": "问题定位", "reference": "标准改法", "reason": "原因剖析"}],
                }
            ],
            "focus_plan": {"name": "最低维度", "days": 7, "material": "选材", "steps": "可执行步骤", "metric": "量化标准", "advice": "7天执行建议"},
            "top_errors": [{"index": "证据编号", "source": "原文", "answer": "原音转写", "problem": "问题"}],
        },
        "rules": [
            "必须覆盖三个维度，且名称与 template 完全一致；顺序按统计中的平均分从低到高。",
            "每个维度至少 2 条证据，证据必须来自提供的 evidence，不得编造。",
            "同一错误只归入一个维度，避免重复扣分。",
            "focus_plan 只能针对最低维度，给出 7 天内可执行的选材、步骤和量化标准。",
            "只返回 JSON，不要 Markdown 代码块。",
        ],
    }
    try:
        raw = await chat(
            [
                {"role": "system", "content": PROMPT + "\n你是资深口译考官和专业复盘教练。严格按 Han (2015) 三个维度做证据化复盘，只返回 JSON。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.35,
        )
        return merge_ai_review(base_result, parse_json(raw))
    except Exception:
        return base_result


@app.post("/api/practice/professional-review")
async def professional_review(req: ProfessionalReviewRequest) -> dict[str, Any]:
    count = max(3, min(20, req.count))
    records = load_practice_records()[:count]
    if len(records) < 3:
        raise HTTPException(422, "至少完成并保存 3 次练习后，才能生成专业复盘。")
    evidence = review_evidence(records)
    dimensions = [
        dimension_report("信息完整性", "信息完整性", evidence, ("content_accuracy",)),
        dimension_report("传递流畅度", "传递流畅度", evidence, ("expression_fluency", "rhythm")),
        dimension_report("目标语言质量", "目标语言质量", evidence, ("wording_structure", "tone")),
    ]
    dimensions.sort(key=lambda item: item["average"])
    overall = round(sum(record.get("review", {}).get("overall_score", 0) for record in records) / len(records))
    previous = load_professional_reviews()
    previous_weakness = previous[0].get("weaknesses", [{}])[0].get("name", "暂无") if previous else "暂无"
    top_errors = [{"index": item["index"], "source": item["source"], "answer": item["answer"], "problem": item["diagnosis"] or "需回看原句与转写"} for item in evidence[:3]]
    result = {
        "count": len(records),
        "overall_score": overall,
        "score_trend": [record.get("review", {}).get("overall_score", 0) for record in reversed(records)],
        "focus_plan": build_focus_plan(dimensions[0]),
        "conclusion": f"本次复盘按 Han (2015) 三维度回看最近 {len(records)} 次练习。当前最低维度是{dimensions[0]['name']}，上次历史复盘的首要弱点是{previous_weakness}。",
        "weaknesses": dimensions,
        "evolution": f"上次首要弱点：{previous_weakness}；本次首要弱点：{dimensions[0]['name']}。若二者不同，说明失分位置已经转移，应停止重复练习旧项目，把7天训练只集中在本次最低维度。",
        "top_errors": top_errors,
        "examiner_prompt": "角色：资深口译考官。必须严格使用 Han (2015) 的信息完整性、传递流畅度、目标语言质量三档描述；每个维度先定档，再引用原音转写、原文、标准改法和原因，不得用笼统评价替代证据。",
    }
    try:
        result = await asyncio.wait_for(
            ai_professional_review(records, evidence, dimensions, overall, previous_weakness, result),
            timeout=85,
        )
    except (Exception, asyncio.TimeoutError):
        pass
    if req.save:
        saved = load_professional_reviews()
        result["id"] = uuid4().hex
        result["created_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        saved.insert(0, result)
        REVIEW_STORE.parent.mkdir(parents=True, exist_ok=True)
        REVIEW_STORE.write_text(json.dumps(saved[:20], ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def load_professional_reviews() -> list[dict[str, Any]]:
    if not REVIEW_STORE.exists():
        return []
    try:
        data = json.loads(REVIEW_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_favorites() -> list[dict[str, Any]]:
    if not FAVORITE_STORE.exists():
        return []
    try:
        data = json.loads(FAVORITE_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_favorites(favorites: list[dict[str, Any]]) -> None:
    FAVORITE_STORE.parent.mkdir(parents=True, exist_ok=True)
    FAVORITE_STORE.write_text(json.dumps(favorites, ensure_ascii=False, indent=2), encoding="utf-8")


def suggestion_text(suggestion: Any) -> str:
    if not isinstance(suggestion, dict):
        return str(suggestion or "")
    return "；".join(
        f"{label}：{suggestion.get(key, '')}"
        for label, key in (("原文定位", "source_quote"), ("问题诊断", "diagnosis"), ("具体改法", "action"), ("修改示例", "example"), ("原因说明", "reason"))
        if suggestion.get(key)
    )


def export_lines(kind: str, record_id: str = "") -> list[str]:
    lines = ["视译练习助手", "来自阿雏苏苏子Liora", ""]
    if kind == "record":
        record = next((item for item in load_practice_records() if item.get("id") == record_id), None)
        if not record:
            raise HTTPException(404, "未找到要导出的练习记录。")
        review = record.get("review", {})
        lines.extend(["单次练习记录", f"时间：{record.get('created_at', '')}", f"方向：{record.get('direction', '')} · 来源：{record.get('source', '')}", f"总分：{review.get('overall_score', '')} · {review.get('overall_label', '')}"])
        feedbacks = {item.get("id"): item.get("feedback", {}) for item in review.get("questions", [])}
        for index, question in enumerate(record.get("questions", []), 1):
            feedback = feedbacks.get(question.get("id"), {})
            lines.extend([f"第 {index} 题", f"题目：{question.get('source', '')}", f"我的作答：{question.get('answer', '')}", f"批改建议：{suggestion_text(feedback.get('suggestion'))}", f"语气：{feedback.get('tone', {}).get('comment', '')}", f"停顿/节奏：{feedback.get('rhythm', {}).get('comment', '')}", ""])
    elif kind == "records":
        lines.append("练习记录")
        for record in load_practice_records():
            review = record.get("review", {})
            lines.extend([f"时间：{record.get('created_at', '')}", f"方向：{record.get('direction', '')} · 来源：{record.get('source', '')}", f"总分：{review.get('overall_score', '')} · {review.get('overall_label', '')}"])
            feedbacks = {item.get("id"): item.get("feedback", {}) for item in review.get("questions", [])}
            for index, question in enumerate(record.get("questions", []), 1):
                feedback = feedbacks.get(question.get("id"), {})
                lines.extend([f"第 {index} 题", f"题目：{question.get('source', '')}", f"我的作答：{question.get('answer', '')}", f"批改：{suggestion_text(feedback.get('suggestion'))}"])
            lines.append("")
    elif kind == "favorites":
        lines.append("收藏与笔记")
        for index, favorite in enumerate(load_favorites(), 1):
            lines.extend([f"收藏 {index} · {favorite.get('created_at', '')}", f"题目：{favorite.get('source', '')}", f"我的作答：{favorite.get('answer', '')}", f"批改：{suggestion_text(favorite.get('feedback', {}).get('suggestion'))}", f"笔记：{favorite.get('note', '')}", ""])
    elif kind == "reviews":
        lines.append("专业复盘")
        for review in load_professional_reviews():
            lines.extend([f"保存时间：{review.get('created_at', '')}", f"样本：最近 {review.get('count', '')} 次练习 · 平均得分 {review.get('overall_score', '')}", f"得分趋势：{' → '.join(map(str, review.get('score_trend', [])))}", f"复盘结论：{review.get('conclusion', '')}", f"弱点演化：{review.get('evolution', '')}"])
            plan = review.get("focus_plan", {})
            lines.extend([f"7天重点：{plan.get('name', '')}", f"选材：{plan.get('material', '')}", f"步骤：{plan.get('steps', '')}", f"量化标准：{plan.get('metric', '')}"])
            for weakness in review.get("weaknesses", []):
                han = weakness.get("han", {})
                lines.extend([f"维度：{weakness.get('name', '')} · {han.get('label', '')} · {weakness.get('average', weakness.get('score', ''))}", weakness.get("diagnosis") or weakness.get("advice", "")])
                for case in weakness.get("cases", []):
                    lines.extend([f"证据 {case.get('index', '')}", f"原音转写：{case.get('original_transcript', '')}", f"原文：{case.get('source', '')}", f"标准改法：{case.get('reference', '')}", f"原因：{case.get('reason', '')}"])
            for error in review.get("top_errors", []):
                lines.extend([f"常犯错误 {error.get('index', '')}", f"原文：{error.get('source', '')}", f"原音转写：{error.get('answer', '')}", f"问题：{error.get('problem', '')}"])
            lines.append("")
    else:
        raise HTTPException(404, "不支持的导出内容。")
    return lines


def make_docx_export(path: Path, lines: list[str]) -> None:
    document = Document()
    document.add_heading(lines[0], 0)
    for line in lines[1:]:
        if not line:
            document.add_paragraph()
        elif line.startswith(("练习记录", "收藏与笔记", "专业复盘", "单次练习记录", "第 ")):
            document.add_heading(line, 1 if not line.startswith("第 ") else 2)
        else:
            document.add_paragraph(line)
    document.save(path)


def make_pdf_export(path: Path, lines: list[str]) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 48
    pdf.setFont("STSong-Light", 10)
    for line in lines:
        words = str(line) or " "
        chunks = [words[i:i + 48] for i in range(0, len(words), 48)] or [""]
        for chunk in chunks:
            if y < 48:
                pdf.showPage()
                pdf.setFont("STSong-Light", 10)
                y = height - 48
            pdf.drawString(42, y, chunk)
            y -= 17
    pdf.save()


@app.get("/api/export/{kind}/{format_name}")
def export_data(kind: str, format_name: str, record_id: str = "") -> FileResponse:
    if format_name not in {"docx", "pdf"}:
        raise HTTPException(404, "不支持的导出格式。")
    EXPORTS.mkdir(parents=True, exist_ok=True)
    filename = f"视译练习助手-{kind}-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.{format_name}"
    path = EXPORTS / filename
    lines = export_lines(kind, record_id)
    if format_name == "docx":
        make_docx_export(path, lines)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        make_pdf_export(path, lines)
        media_type = "application/pdf"
    return FileResponse(path, media_type=media_type, filename=filename)


@app.post("/api/practice/favorites")
def favorite_create(req: FavoriteRequest) -> dict[str, Any]:
    record = next((item for item in load_practice_records() if item.get("id") == req.record_id), None)
    if not record:
        raise HTTPException(404, "关联的练习记录不存在。")
    question = next((item for item in record.get("questions", []) if item.get("id") == req.question_id), None)
    if not question:
        raise HTTPException(404, "关联的题目不存在。")
    favorites = load_favorites()
    if any(item.get("record_id") == req.record_id and item.get("question_id") == req.question_id for item in favorites):
        raise HTTPException(409, "该题已收藏。")
    feedback = next((item.get("feedback", {}) for item in record.get("review", {}).get("questions", []) if item.get("id") == req.question_id), {})
    favorite = {"id": uuid4().hex, "record_id": req.record_id, "question_id": req.question_id, "created_at": datetime.now(TZ).isoformat(timespec="seconds"), "source": question.get("source", ""), "answer": question.get("answer", ""), "feedback": feedback, "note": req.note.strip()}
    favorites.insert(0, favorite)
    save_favorites(favorites)
    return favorite


@app.get("/api/practice/favorites")
def favorites_list() -> dict[str, Any]:
    return {"favorites": load_favorites()}


@app.put("/api/practice/favorites/{favorite_id}")
def favorite_update(favorite_id: str, req: FavoriteRequest) -> dict[str, Any]:
    favorites = load_favorites()
    favorite = next((item for item in favorites if item.get("id") == favorite_id), None)
    if not favorite:
        raise HTTPException(404, "未找到收藏。")
    favorite["note"] = req.note.strip()
    save_favorites(favorites)
    return favorite


@app.delete("/api/practice/favorites/{favorite_id}")
def favorite_delete(favorite_id: str) -> dict[str, Any]:
    favorites = load_favorites()
    remaining = [item for item in favorites if item.get("id") != favorite_id]
    if len(remaining) == len(favorites):
        raise HTTPException(404, "未找到收藏。")
    save_favorites(remaining)
    return {"message": "已取消收藏。"}


@app.get("/api/practice/professional-reviews")
def professional_reviews_list() -> dict[str, Any]:
    return {"reviews": load_professional_reviews()[:20]}


@app.delete("/api/practice/professional-reviews/{review_id}")
def professional_review_delete(review_id: str) -> dict[str, Any]:
    reviews = load_professional_reviews()
    remaining = [item for item in reviews if item.get("id") != review_id]
    if len(remaining) == len(reviews):
        raise HTTPException(404, "未找到这份历史复盘。")
    REVIEW_STORE.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"message": "历史复盘已删除。"}


@app.get("/api/practice/records/{record_id}")
def practice_record_detail(record_id: str) -> dict[str, Any]:
    record = next((item for item in load_practice_records() if item.get("id") == record_id), None)
    if not record:
        raise HTTPException(404, "未找到这条练习记录。")
    return record


@app.delete("/api/practice/records/{record_id}")
def practice_record_delete(record_id: str) -> dict[str, Any]:
    records = load_practice_records()
    remaining = [item for item in records if item.get("id") != record_id]
    if len(remaining) == len(records):
        raise HTTPException(404, "未找到这条练习记录。")
    save_practice_records(remaining)
    return {"message": "练习记录已删除。"}


@app.delete("/api/practice/records")
def practice_records_clear() -> dict[str, Any]:
    save_practice_records([])
    return {"message": "练习记录已清空。"}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "has_key": bool(API_KEY)}
