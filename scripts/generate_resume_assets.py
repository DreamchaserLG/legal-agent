from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "resume_outputs"
SOURCE_IMAGE = Path(r"C:\Users\李顾\Desktop\简历\李固-简历.png")
FONT_REGULAR = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")
FONT_SERIF = Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\simhei.ttf")

W, H = 1654, 2339
MARGIN_X = 92
MARGIN_Y = 70
CONTENT_W = W - MARGIN_X * 2

INK = (24, 31, 42)
MUTED = (73, 86, 101)
LIGHT = (244, 247, 250)
LINE = (210, 219, 230)
BLUE = (28, 83, 156)
TEAL = (0, 128, 128)
GREEN = (45, 115, 83)


def font(size: int, bold: bool = False, serif: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else (FONT_SERIF if serif else FONT_REGULAR)
    return ImageFont.truetype(str(path), size=size)


FONTS = {
    "name": font(62, bold=True),
    "role": font(28, bold=True),
    "meta": font(25),
    "section": font(31, bold=True),
    "sub": font(27, bold=True),
    "body": font(25),
    "body_b": font(25, bold=True),
    "small": font(21),
    "tag": font(20, bold=True),
}


def text_w(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> int:
    return math.ceil(draw.textbbox((0, 0), text, font=fnt)[2])


def text_h(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=fnt)
    return math.ceil(box[3] - box[1])


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt: ImageFont.FreeTypeFont,
    width: int,
    indent: int = 0,
) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        current = ""
        for ch in para:
            trial = current + ch
            current_width = width - (indent if lines and lines[-1].startswith("  ") else 0)
            if current and text_w(draw, trial, fnt) > current_width:
                lines.append(current)
                current = ch
            else:
                current = trial
        if current:
            lines.append(current)
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fnt: ImageFont.FreeTypeFont,
    width: int,
    fill=INK,
    line_gap: int = 8,
    bullet: bool = False,
) -> int:
    x, y = xy
    prefix = "• " if bullet else ""
    prefix_w = text_w(draw, prefix, fnt)
    lines = wrap_text(draw, text, fnt, width - prefix_w)
    for i, line in enumerate(lines):
        if i == 0 and bullet:
            draw.text((x, y), prefix, font=fnt, fill=fill)
            draw.text((x + prefix_w, y), line, font=fnt, fill=fill)
        elif bullet:
            draw.text((x + prefix_w, y), line, font=fnt, fill=fill)
        else:
            draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def draw_rich_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    runs: Sequence[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]],
) -> None:
    cursor = x
    for text, fnt, fill in runs:
        draw.text((cursor, y), text, font=fnt, fill=fill)
        cursor += text_w(draw, text, fnt)


def crop_photo() -> Image.Image | None:
    if not SOURCE_IMAGE.exists():
        return None
    source = Image.open(SOURCE_IMAGE).convert("RGB")
    photo = source.crop((676, 88, 800, 242))
    return photo.resize((158, 198), Image.Resampling.LANCZOS)


def section(draw: ImageDraw.ImageDraw, y: int, title: str, accent=BLUE) -> int:
    draw.rounded_rectangle((MARGIN_X, y, W - MARGIN_X, y + 48), radius=4, fill=LIGHT)
    draw.rectangle((MARGIN_X, y, MARGIN_X + 10, y + 48), fill=accent)
    draw.text((MARGIN_X + 22, y + 6), title, font=FONTS["section"], fill=INK)
    return y + 64


def item_header(
    draw: ImageDraw.ImageDraw,
    y: int,
    left: str,
    right: str = "",
    middle: str = "",
) -> int:
    draw.text((MARGIN_X, y), left, font=FONTS["sub"], fill=INK)
    if middle:
        mw = text_w(draw, middle, FONTS["sub"])
        draw.text((MARGIN_X + (CONTENT_W - mw) // 2, y), middle, font=FONTS["sub"], fill=INK)
    if right:
        rw = text_w(draw, right, FONTS["sub"])
        draw.text((W - MARGIN_X - rw, y), right, font=FONTS["sub"], fill=INK)
    return y + 40


def bullets(draw: ImageDraw.ImageDraw, y: int, lines: Iterable[str], width: int = CONTENT_W) -> int:
    for line in lines:
        y = draw_wrapped(draw, (MARGIN_X + 8, y), line, FONTS["body"], width - 8, fill=INK, bullet=True)
        y += 1
    return y


def draw_header(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    role: str,
    accent=BLUE,
) -> int:
    x, y = MARGIN_X, MARGIN_Y
    draw.text((x, y), "李固", font=FONTS["name"], fill=INK)
    draw.text((x + 148, y + 27), role, font=FONTS["role"], fill=accent)
    y += 90
    draw.text(
        (x, y),
        "男 | 25岁 | 河南商丘 | 18638439093 | 3453510037@qq.com | 2027届硕士",
        font=FONTS["meta"],
        fill=MUTED,
    )
    y += 34
    draw.text(
        (x, y),
        "南京信息工程大学 计算机技术（专硕） | NLP / 大语言模型 / 情感分析",
        font=FONTS["meta"],
        fill=MUTED,
    )

    photo = crop_photo()
    if photo:
        px, py = W - MARGIN_X - 158, MARGIN_Y
        draw.rectangle((px - 5, py - 5, px + 163, py + 203), outline=LINE, width=3)
        img.paste(photo, (px, py))

    draw.line((MARGIN_X, MARGIN_Y + 222, W - MARGIN_X, MARGIN_Y + 222), fill=accent, width=3)
    return MARGIN_Y + 250


def draw_tags(draw: ImageDraw.ImageDraw, y: int, tags: Sequence[str], accent=BLUE) -> int:
    x = MARGIN_X
    max_x = W - MARGIN_X
    for tag in tags:
        tw = text_w(draw, tag, FONTS["tag"])
        if x + tw + 30 > max_x:
            x = MARGIN_X
            y += 42
        draw.rounded_rectangle((x, y, x + tw + 30, y + 34), radius=5, fill=(235, 242, 252), outline=(198, 214, 236))
        draw.text((x + 15, y + 5), tag, font=FONTS["tag"], fill=accent)
        x += tw + 44
    return y + 50


def draw_footer(draw: ImageDraw.ImageDraw, accent=BLUE) -> None:
    draw.line((MARGIN_X, H - 62, W - MARGIN_X, H - 62), fill=LINE, width=1)
    draw.text((MARGIN_X, H - 46), "求职方向：算法工程师 / NLP / 大模型应用", font=FONTS["small"], fill=MUTED)
    mark = "Resume tailored by role"
    draw.text((W - MARGIN_X - text_w(draw, mark, FONTS["small"]), H - 46), mark, font=FONTS["small"], fill=MUTED)


def save(img: Image.Image, name: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png = OUT_DIR / f"{name}.png"
    pdf = OUT_DIR / f"{name}.pdf"
    img.save(png, dpi=(200, 200))
    img.convert("RGB").save(pdf, "PDF", resolution=200.0)
    print(png)
    print(pdf)


def render_algorithm() -> None:
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, img, "NLP / 大模型算法工程师", BLUE)

    y = section(draw, y, "教育背景", BLUE)
    y = item_header(draw, y, "南京信息工程大学", "计算机技术（硕士在读）", "2024.09 - 2027.06")
    y = draw_wrapped(draw, (MARGIN_X + 8, y), "研究方向：自然语言处理、大语言模型、方面级情感分析；发表 SCI 三区论文 1 篇。", FONTS["body"], CONTENT_W - 8, fill=MUTED, bullet=True)
    y += 4
    y = item_header(draw, y, "安阳工学院", "软件工程（本科）", "2020.09 - 2024.06")
    y = draw_wrapped(draw, (MARGIN_X + 8, y), "绩点 3.33，专业前 5%；主修数据结构、算法、操作系统、数据库、机器学习基础。", FONTS["body"], CONTENT_W - 8, fill=MUTED, bullet=True)
    y += 12

    y = section(draw, y, "研究与算法能力", BLUE)
    y = draw_tags(draw, y, ["ABSA", "Sentiment Analysis", "Transformer", "LoRA", "Prompt Tuning", "PyTorch", "HuggingFace"], BLUE)
    y = bullets(draw, y, [
        "聚焦细粒度情感分析与情感极性判别，能围绕数据集、损失函数、消融实验和误差案例建立完整实验闭环。",
        "熟悉 Transformer、注意力机制、预训练语言模型微调、Prompt/LoRA 等常见大模型适配方法。",
        "数学基础扎实，获全国大学生数学竞赛省一等奖；具备线性代数、概率统计、最优化与建模推导能力。",
    ])
    y += 10

    y = section(draw, y, "论文与学术成果", BLUE)
    y = item_header(draw, y, "SSICE: Sentiment Subspace-guided Slot Injection and Context Enhancement for Sentiment Analysis", "SCI三区", "")
    y = bullets(draw, y, [
        "提出情感子空间建模思路，将情感极性相关信息映射到更可分的语义表示空间，强化细粒度情感特征抽取。",
        "设计槽位注入与上下文增强模块，把显式情感知识融入句子表征，提升模型对隐式情感线索的捕获能力。",
        "完成公开数据集实验、消融实验与案例分析，对比验证各模块对极性识别效果的贡献。",
    ])
    y += 10

    y = section(draw, y, "项目经验", BLUE)
    y = item_header(draw, y, "国外法律案件分析与预测 Agent 系统", "校级合作项目", "2026.03 - 2026.06")
    y = bullets(draw, y, [
        "将案情文本拆解为关键事实、争议焦点、法律关系和检索关键词，构建证据优先的 RAG 分析流程。",
        "实现 PostgreSQL full-text + pgvector/HNSW 向量检索 + 本地 rerank 的混合检索链路，支持结构化过滤与 RRF 融合。",
        "围绕检索召回、证据上下文、模型置信度和不确定性提示优化输出，降低脱离材料生成结论的风险。",
    ])
    y += 6
    y = item_header(draw, y, "路面裂缝检测识别系统", "本科毕业设计/项目", "2023.03 - 2023.09")
    y = bullets(draw, y, [
        "基于 MATLAB 完成图像预处理、阈值分割、连通区域分析和几何参数计算，实现裂缝类型识别与结果可视化。",
    ])
    y += 10

    y = section(draw, y, "荣誉与技术栈", BLUE)
    y = bullets(draw, y, [
        "竞赛/荣誉：蓝桥杯省二等奖、数学竞赛省一等奖、数学建模省二等奖（队长）、高校计算机能力挑战赛华中区一等奖、国家励志奖学金、三好学生。",
        "技能：Python、C、Java、PyTorch、Transformers、LangChain、FastAPI、Spring Boot、MySQL、PostgreSQL、Linux、Docker、Git、LaTeX。",
    ])
    draw_footer(draw, BLUE)
    save(img, "李固-算法岗简历")


def render_agent() -> None:
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, img, "Agent 开发 / 大模型应用工程师", TEAL)

    y = section(draw, y, "教育背景", TEAL)
    y = item_header(draw, y, "南京信息工程大学", "计算机技术（硕士在读）", "2024.09 - 2027.06")
    y = draw_wrapped(draw, (MARGIN_X + 8, y), "方向：NLP、大语言模型、情感分析；论文 SSICE（SCI三区）。", FONTS["body"], CONTENT_W - 8, fill=MUTED, bullet=True)
    y += 4
    y = item_header(draw, y, "安阳工学院", "软件工程（本科）", "2020.09 - 2024.06")
    y = draw_wrapped(draw, (MARGIN_X + 8, y), "绩点 3.33，专业前 5%；具备扎实的数据结构、数据库、后端开发与工程实现基础。", FONTS["body"], CONTENT_W - 8, fill=MUTED, bullet=True)
    y += 12

    y = section(draw, y, "核心能力", TEAL)
    y = draw_tags(draw, y, ["RAG", "Hybrid Search", "pgvector/HNSW", "LLM API", "FastAPI", "Docker", "PostgreSQL", "Redis"], TEAL)
    y = bullets(draw, y, [
        "熟悉大模型应用链路：Prompt Engineering、RAG、工具调用、结构化输出、多轮对话上下文与结果校验。",
        "具备后端与数据工程能力：FastAPI/Flask/Spring Boot、RESTful API、MySQL/PostgreSQL/Redis、Linux 与 Docker 部署。",
        "能把业务问题拆成可执行子任务，围绕检索、重排序、证据组织、生成约束和指标评估做工程闭环。",
    ])
    y += 10

    y = section(draw, y, "项目经验", TEAL)
    y = item_header(draw, y, "国外法律案件分析与预测 Agent 系统", "校级合作项目", "2026.03 - 2026.06")
    y = bullets(draw, y, [
        "负责法律智能检索与分析链路：案情解析、关键词规划、法规/案例检索、证据包组织、风险点总结与预测辅助。",
        "搭建 PostgreSQL + pgvector 数据底座，完成法规 XML 入库、RAG chunk 重建、HNSW cosine 索引和结构化过滤。",
        "实现关键词检索 + 向量检索 + RRF 融合 + 本地法律 rerank；优化后关键词检索约 112-894ms，混合检索约 335-926ms。",
        "接入 Qwen OpenAI-compatible 调用，要求回答基于检索证据输出，并显式给出不确定性、缺失事实与置信度边界。",
        "封装 FastAPI 页面与 API，提供 CLI 管理脚本、环境模板、部署说明和最小可运行交付包。",
    ])
    y += 6

    y = item_header(draw, y, "智能面试系统 / 面试题库问答原型", "LLM应用练习", "2025.05 - 2025.07")
    y = bullets(draw, y, [
        "围绕题库问答设计 RAG 流程，按岗位、知识点和难度组织检索上下文；结合情感分析能力辅助候选人状态评估。",
        "使用 FastAPI + Redis 缓存接口结果，前端与后端联调实现低延迟问答与记录回看。",
    ])
    y += 6

    y = item_header(draw, y, "路面裂缝检测识别系统", "图像处理项目", "2023.03 - 2023.09")
    y = bullets(draw, y, [
        "使用 MATLAB GUIDE 构建 GUI，集成预处理、分割、特征提取、几何参数计算和可视化模块。",
    ])
    y += 10

    y = section(draw, y, "技术栈与荣誉", TEAL)
    y = bullets(draw, y, [
        "语言/框架：Python、Java、C、FastAPI、Flask、Spring Boot、LangChain、PyTorch、Transformers。",
        "数据/部署：PostgreSQL、pgvector、MySQL、Redis、Docker、Linux、Git、Nginx（基础）、GitHub Actions（基础）。",
        "荣誉：蓝桥杯省二等奖、数学竞赛省一等奖、数学建模省二等奖（队长）、高校计算机能力挑战赛华中区一等奖、CET-4、计算机三级、参编国家规划教材《C语言程序设计》。"
    ])
    draw_footer(draw, TEAL)
    save(img, "李固-Agent开发简历")


def main() -> None:
    render_algorithm()
    render_agent()


if __name__ == "__main__":
    main()
