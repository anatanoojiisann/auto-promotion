import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

root = Path("outputs/facebook-formal-20261026").resolve()
research = root / "research"
zone = ZoneInfo("Asia/Shanghai")


def read(name):
    return json.loads((research / (name + ".value.json")).read_text(encoding="utf-8"))


def clean(url):
    u = urlsplit(url)
    q = parse_qs(u.query)
    return urlunsplit(
        (
            u.scheme,
            u.netloc,
            u.path,
            urlencode(
                {
                    k: v[0]
                    for k, v in q.items()
                    if k in {"comment_id", "reply_comment_id", "story_fbid", "id"}
                }
            ),
            "",
        )
    )


def parent(url):
    return url.split("?", 1)[0]


def chinese_time(value):
    m = re.search(r"(\d{4})年(\d+)月(\d+)日.*?(\d{2}):(\d{2})", value or "")
    return datetime(*map(int, m.groups()), tzinfo=zone).isoformat() if m else None


comments = read("facebook-shortlist-comments-read")
comfy = comments[0]
training = comments[1]
long_video = read("facebook-video-followups-loaded")
reviews = read("facebook-shortlist-review-open")
calls = read("facebook-ai-tools-current")
jesse = read("facebook-details-loaded")[0]
items = []


def add_post(source, url, author, title, quote, relative, reason, resolution, category="ai_video"):
    if isinstance(source, dict):
        body = source.get("text", "")
    else:
        body = str(source)
    assert quote in body, (title, "quote absent")
    items.append(
        dict(
            priority="P1",
            kind="post",
            author=author,
            title=title,
            url=clean(url),
            parent_url=parent(url),
            source_quote=quote,
            published_at=None,
            event_date="2026-10-26",
            time_evidence=relative,
            timestamp_precision="relative",
            reason=reason,
            resolution_check=resolution,
            category=category,
            coverage="partial",
            sending_started=False,
        )
    )


def add_comment(source, comment_id, author, title, quote, reason, resolution, category="ai_video"):
    row = next(c for c in source["comments"] if any(comment_id in a["url"] for a in c["links"]))
    link = next(a for a in row["links"] if comment_id in a["url"])
    assert quote in row["text"], title
    published = chinese_time(link["label"])
    assert published and published.startswith("2026-10-26"), title
    items.append(
        dict(
            priority="P1",
            kind="comment",
            author=author,
            title=title,
            url=clean(link["url"]),
            parent_url=parent(link["url"]),
            source_quote=quote,
            published_at=published,
            event_date="2026-10-26",
            time_evidence=link["label"],
            timestamp_precision="minute",
            reason=reason,
            resolution_check=resolution,
            category=category,
            coverage="partial",
            sending_started=False,
        )
    )


add_post(
    reviews[0],
    reviews[0]["url"],
    "Tony Williams",
    "视频中太阳位置与光照无法保持一致",
    "I am having issues with the sun changing positions even when I type in negative prompts to prevent this",
    "今日；最终读取时页面显示约1小时前",
    "明确描述负面提示词无法维持太阳位置，存在具体生成一致性问题。",
    "读取范围内尚未见解决回复。",
)
add_post(
    reviews[1],
    reviews[1]["url"],
    "Tony Williams",
    "Seedance 2.5 草稿模式是否会恢复",
    "Does anyone know if Higgsfield going to bring back the Seedance 2.5 draft, or will they remove it completely?",
    "今日；最终读取时页面显示16分钟前",
    "明确询问曾依赖的草稿模式能否恢复，存在工具可用性需求。",
    "读取范围内尚未见解决回复；平台现状需在拟答时另行核实。",
)
add_post(
    jesse,
    jesse["url"],
    "Jesse Tee",
    "寻找免费制作怀孕揭晓视频的网站",
    "What site can I use to create a pregnancy reveal video at no cost?",
    "今日；详情读取时页面显示2小时前",
    "有明确用途、视频制作目标和零成本条件。",
    "已有泛泛的试用或本地开源建议，尚未见具体免费网站方案或作者确认。",
)
add_comment(
    comfy,
    "1117166117722754",
    "Al Rousseau",
    "索要视频生成提示词的组织方法",
    "I would love to know how you structure your prompt(s).",
    "直接请求具体提示词结构，属于可操作的制作方法需求。",
    "已读取主评论，尚未见对应方法答复。",
)
add_comment(
    comfy,
    "1117328011039898",
    "Justin Wilson",
    "询问如何将生成视频放大至1080p",
    "How are you managing to upscale to 1080p?",
    "明确询问生成视频后处理步骤和目标分辨率。",
    "已读取主评论，尚未见对应放大步骤答复。",
)
add_comment(
    long_video,
    "1148580157847916",
    "Julian Brana",
    "询问制作3.5分钟AI视频的实际成本",
    "Can I ask how much this cost you to make?",
    "对特定较长视频询问制作成本，具有明确预算评估需求。",
    "后续答复提到工具组合，未给出费用。",
)
add_comment(
    long_video,
    "1148606341178631",
    "Jimmy Finley",
    "索要长视频制作指南与硬件要求",
    "did you use super computer and any guide",
    "明确索要完成较长高质量视频的指南，并询问硬件门槛。",
    "后续答复仅列出工具和模型，仍缺可复用步骤及硬件说明。",
)
add_post(
    reviews[2],
    reviews[2]["url"],
    "Anatoli TonisBit",
    "AI商业短片报价与客户价值展示",
    "How much do you charge for this kind of project",
    "今日；最终读取时原帖显示11小时前，作者追加问题显示2小时前",
    "作者给出已有项目及当前报价，继续询问客户犹豫来自价格还是价值证明不足，需求具体。",
    "有一条泛行业费率建议；作者之后提出的自身定价及价值证明问题尚未见答复。",
)
ali = next(a for a in calls["articles"] if any("1765398241616670" in x["url"] for x in a["links"]))
add_post(
    ali,
    "https://www.facebook.com/groups/698593531630485/posts/1765398241616670/",
    "Ali Mirza",
    "寻找电话或WhatsApp通话笔记工具",
    "any AI tool that can take notes from our phone calls or whatsapp calls?",
    "今日；读取时原帖显示7小时前",
    "明确要求通话笔记能力和电话/WhatsApp场景，属于泛AI工具需求。",
    "已有工具和外放录音方案；尚未见作者确认，须先核实设备及所需的通话接入方式。",
    "extended_ai_need",
)
add_comment(
    training,
    "1123058973563014",
    "Priscilla Modiga",
    "寻找类似的AI工具实操培训",
    "Where to find similar training",
    "在已过期的AI工具实操活动下明确寻找当前同类培训资源，属于泛AI学习需求。",
    "原活动已过期，读取全部3条主评论时未见新培训资源答复。",
    "extended_ai_need",
)

assert len(items) == 10 and len({i["url"] for i in items}) == 10
counts = Counter(i["parent_url"] for i in items)
for index, item in enumerate(items, 1):
    item["number"] = index
    item["event_id"] = "fb-" + hashlib.sha256(item["url"].encode()).hexdigest()[:24]
    item["same_parent_candidates"] = counts[item["parent_url"]]
    item["dispatch_note"] = (
        "同一原帖只选择一个入口，不对同一原帖连续或跨账号重复互动。"
        if counts[item["parent_url"]] > 1
        else "拟答及发送前重新核验上下文、产品事实、权限和时间。"
    )
    item["product_match"] = (
        "视频创作需求；具体产品/版本/费用事实需拟答时核实。"
        if item["category"] == "ai_video"
        else "扩展AI工具/学习需求；与当前推广产品的匹配尚未确认。"
    )
manifest = {
    "date": "2026-10-26",
    "timezone": "Asia/Shanghai",
    "generated_at": datetime.now(zone).isoformat(),
    "scope": "Facebook only; today means the post or comment event was newly published on 2026-10-26 UTC+8",
    "p0_count": 0,
    "p1_candidate_count": 10,
    "post_count": 5,
    "comment_count": 5,
    "parent_post_count": len(counts),
    "video_creation_candidates": 8,
    "extended_ai_candidates": 2,
    "coverage": "partial",
    "execution_started": False,
    "classification_note": "P1表示可操作且在读取范围内仍有缺口的明确需求候选；不是已通过发送资格的条目。未核实的产品匹配或平台事实不作承诺。",
    "items": items,
}
(root / "today-p1-candidates.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
)
opportunities = []
for item in items:
    context = json.dumps(
        {
            "priority": item["priority"],
            "kind": item["kind"],
            "source_quote": item["source_quote"],
            "event_date": item["event_date"],
            "time_evidence": item["time_evidence"],
            "timestamp_precision": item["timestamp_precision"],
            "reason": item["reason"],
            "resolution_check": item["resolution_check"],
            "product_match": item["product_match"],
            "parent_url": item["parent_url"],
            "dispatch_note": item["dispatch_note"],
            "coverage": "partial",
            "execution_started": False,
        },
        ensure_ascii=False,
    )
    opportunities.append(
        {
            "platform": "facebook",
            "external_id": item["event_id"],
            "target": item["url"],
            "action": "reply" if item["kind"] == "comment" else "comment",
            "author_id": item["author"],
            "title": item["title"],
            "context": context,
            "language": "en",
            "topics": ["P1", item["category"], "2026-10-26", "discovery_only"],
            "confidence": 0.85 if item["category"] == "ai_video" else 0.75,
            "value": 0.8 if item["category"] == "ai_video" else 0.6,
            "is_public": True,
            "permission": "unknown",
            "published_at": item["published_at"],
            "expires_at": "2026-10-26T23:59:59+08:00",
        }
    )
grouped = {}
for candidate, opportunity in zip(items, opportunities):
    grouped.setdefault(candidate["parent_url"], []).append((candidate, opportunity))
grouped_opportunities = []
for parent_url, rows in grouped.items():
    value = dict(rows[0][1])
    value["title"] = " / ".join(c["title"] for c, _ in rows)
    value["author_id"] = " / ".join(c["author"] for c, _ in rows)
    value["context"] = json.dumps(
        {
            "priority": "P1",
            "event_date": "2026-10-26",
            "candidate_event_count": len(rows),
            "parent_url": parent_url,
            "execution_started": False,
            "coverage": "partial",
            "dispatch_note": "同一原帖只选择一个互动入口，不跨账号重复互动。",
            "events": [
                {
                    **json.loads(o["context"]),
                    "event_id": c["event_id"],
                    "url": c["url"],
                    "author": c["author"],
                    "title": c["title"],
                    "published_at": c["published_at"],
                }
                for c, o in rows
            ],
        },
        ensure_ascii=False,
    )
    grouped_opportunities.append(value)
(root / "opportunities-import.json").write_text(
    json.dumps({"items": grouped_opportunities}, ensure_ascii=False, indent=2), encoding="utf-8"
)
migration = json.loads((root / "migration-report.json").read_text(encoding="utf-8"))


def esc(value):
    return html.escape(str(value))


cards = "".join(
    f'''<article><div class="row"><span class="badge">P1 · {("新帖" if i["kind"] == "post" else "新评论")}</span><span class="time">{esc(i["time_evidence"])}</span></div><h3>{i["number"]:02d} · <a href="{esc(i["url"])}" target="_blank" rel="noopener noreferrer">{esc(i["title"])}</a></h3><p class="author">{esc(i["author"])}</p><blockquote>{esc(i["source_quote"])}</blockquote><p>{esc(i["reason"])}</p><p class="muted">核对：{esc(i["resolution_check"])}</p><p class="muted">{esc(i["product_match"])}</p>{('<p class="shared">关联提醒：' + esc(i["dispatch_note"]) + "</p>") if i["same_parent_candidates"] > 1 else ""}</article>'''
    for i in items
)
page = (
    """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Facebook 正式接入与今日候选</title><style>
body{margin:0;background:#f4f7fb;color:#172640;font:15px/1.7 "Microsoft YaHei",system-ui,sans-serif}main{max-width:1080px;margin:auto;padding:40px 24px}h1{font-size:30px;margin:0 0 8px}h2{font-size:21px;margin-top:32px}h3{font-size:18px;margin:8px 0}a{color:#1856a5;text-decoration:none}a:hover{text-decoration:underline}.subtitle,.muted,.author,.time{color:#617087}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.metric{background:#fff;border:1px solid #dbe4ef;border-radius:12px;padding:16px}.metric strong{font-size:25px;display:block;color:#154b89}.panel,article{background:white;border:1px solid #dbe4ef;border-radius:12px;padding:22px;margin:16px 0}.row{display:flex;justify-content:space-between;gap:15px}.badge{color:#126045;background:#e7f5ed;border-radius:20px;padding:2px 10px;font-weight:600}.time{font-size:12px}.author{margin:0 0 10px}blockquote{margin:14px 0;padding:12px 16px;background:#f5f8fc;border-left:3px solid #7398c7;color:#344963}.shared{font-size:13px;background:#fff5dc;color:#825a13;padding:8px 12px;border-radius:6px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px;border-bottom:1px solid #e3eaf3}code{font-size:12px;overflow-wrap:anywhere}.foot{font-size:12px;color:#6c7b90;margin-top:28px}@media(max-width:720px){.summary{grid-template-columns:repeat(2,1fr)}.row{display:block}.time{display:block;margin-top:6px}main{padding:24px 14px}}</style><main>
<h1>Facebook 正式接入与今日候选</h1><p class="subtitle">北京时间 2026-10-26 · 只读发现与候选入库 · 未发送新互动</p>
<div class="summary"><div class="metric"><strong>2</strong>Facebook 正式账号</div><div class="metric"><strong>30</strong>保留历史记录</div><div class="metric"><strong>10</strong>P1 需求候选</div><div class="metric"><strong>0</strong>本次新增发送</div></div>
<section class="panel"><h2 style="margin-top:0">正式迁移结果</h2><table><tr><th>账号</th><th>原连接</th><th>历史状态</th></tr><tr><td>Kore Fluigens</td><td>原 Tabbit 登录</td><td>13 已确认、2 取消/跳过；原停止状态保留</td></tr><tr><td>Loo William</td><td>原 Chrome 登录</td><td>2 已确认、12 取消/跳过、1 结果未明；未重发</td></tr></table><p>迁移已先备份；原历史文件哈希一致，重复登记不会新增记录。迁移没有新增尝试或执行队列，原停止开关、配额和其他账号节点范围均保留。Instagram 未在本次正式迁移范围内。</p><p class="muted">云端版本：<code>4c7b853a-ba44-488e-b8c8-f395e0b5d174</code>。验收：云端 121 项、Meta 13 项、管理接口 10 项、任务通道 17 项通过。旧任务与桌面派发器均未重新启动。</p></section>
<h2>今天发布的 P1 候选</h2><p>5 条今日新帖 + 5 条今日新增评论，分布在 8 个原帖下。其中 8 条与视频创作直接相关，2 条为扩展 AI 工具/学习需求。本次可见通知没有发现今天的 P0 求助。旧原帖上的候选仅按今天新发布的评论计数。</p><p class="muted">这是需求候选清单。评论覆盖为部分读取；已有建议但未确认闭环的条目已注明。发送前仍需重新检查具体产品匹配、最新上下文和权限；关联原帖只选择一个互动入口。</p>
"""
    + cards
    + f"""<p class="foot">生成时间：{esc(manifest["generated_at"])}。精确到分钟的评论时间来自 Facebook 页面标签；新帖保留原相对时间，不虚构精确发布时间。原文和链接证据位于同目录 research 文件夹。</p></main></html>"""
)
(root / "Facebook正式接入与今日10条P1候选.html").write_text(page, encoding="utf-8")
print(
    json.dumps(
        {
            k: manifest[k]
            for k in (
                "p0_count",
                "p1_candidate_count",
                "post_count",
                "comment_count",
                "parent_post_count",
                "video_creation_candidates",
                "extended_ai_candidates",
                "execution_started",
            )
        },
        ensure_ascii=False,
    )
)
