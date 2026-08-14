"""更多技能市场：内置技能 + 从 GitHub（镜像）下载 SKILL.md。

- grill-me 等来自真实 GitHub 仓库，下载时按镜像顺序轮询。
- 下载成功后存到 data/skills/<id>/SKILL.md，并写入 data/user_skills.json。
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

from . import db  # noqa: F401 —— 仅确保 DATA_DIR 初始化习惯一致

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SKILLS_DIR = DATA_DIR / "skills"

_GRILL = (
    "---\nname: grill-me\ndescription: 连续追问用户关于计划/设计/方案的每一个分支决策，"
    "直到达成共同理解。仅当用户明确说 grill me / 拷打我 / 挑战这个方案时触发。\n---\n\n"
    "# grill-me\n\n用苏格拉底式追问压力测试用户的想法：\n"
    "1. 一次只问一个问题，追问最不确定的分支。\n"
    "2. 复述用户当前立场，再指出漏洞、假设和未考虑的代价。\n"
    "3. 每个分支解决后明确标记，再进入下一个分支。\n"
    "4. 不替用户做决定；用户要求结束时，给出决策摘要。\n"
)

CATALOG: list[dict] = [
    {
        "id": "grill-me",
        "name": "grill-me 拷问模式",
        "desc": "连续苏格拉底式追问，把方案或设计的每个分支决策逼到清晰。",
        "source": "github",
        "repo": "RobMitt/grill-me-skill",
        "file": "SKILL.md",
        "icon": "🔍",
    },
    {
        "id": "skill-creator",
        "name": "技能创建器",
        "desc": "指导编写结构化 SKILL.md：元数据、触发条件、步骤与示例。",
        "source": "github",
        "repo": "anthropics/skills",
        "file": "skill-creator/SKILL.md",
        "icon": "🛠️",
    },
    {
        "id": "pdf",
        "name": "PDF 处理",
        "desc": "生成和解析 PDF 文档的技能（来自 Anthropic skills）。",
        "source": "github",
        "repo": "anthropics/skills",
        "file": "pdf/SKILL.md",
        "icon": "📄",
    },
    {
        "id": "docx", "name": "Word 文档", "source": "github", "repo": "anthropics/skills",
        "file": "docx/SKILL.md", "icon": "📝",
        "desc": "生成和编辑 .docx 文档（Anthropic skills）。",
    },
    {
        "id": "xlsx", "name": "Excel 表格", "source": "github", "repo": "anthropics/skills",
        "file": "xlsx/SKILL.md", "icon": "📊",
        "desc": "生成和编辑 .xlsx 表格，支持公式与格式（Anthropic skills）。",
    },
    {
        "id": "pptx", "name": "PowerPoint 演示", "source": "github", "repo": "anthropics/skills",
        "file": "pptx/SKILL.md", "icon": "🎞️",
        "desc": "生成和编辑 .pptx 演示文稿（Anthropic skills）。",
    },
    {
        "id": "brand-guidelines", "name": "品牌规范", "source": "github", "repo": "anthropics/skills",
        "file": "brand-guidelines/SKILL.md", "icon": "🎨",
        "desc": "应用品牌色彩、字体与视觉规范（Anthropic skills）。",
    },
    {
        "id": "artifacts-builder", "name": "Artifacts 构建器", "source": "github", "repo": "anthropics/skills",
        "file": "artifacts-builder/SKILL.md", "icon": "🧱",
        "desc": "构建复杂的多文件 Claude Artifacts（Anthropic skills）。",
    },
    {
        "id": "mcp-builder", "name": "MCP 构建器", "source": "github", "repo": "anthropics/skills",
        "file": "mcp-builder/SKILL.md", "icon": "🔧",
        "desc": "创建 MCP 服务器，连接外部工具与数据（Anthropic skills）。",
    },
    {
        "id": "webapp-testing", "name": "Web 应用测试", "source": "github", "repo": "anthropics/skills",
        "file": "webapp-testing/SKILL.md", "icon": "🧪",
        "desc": "用 Playwright 对 Web 应用做端到端测试（Anthropic skills）。",
    },
    {
        "id": "frontend-design", "name": "前端设计", "source": "github", "repo": "vercel-labs/skills",
        "file": "frontend-design/SKILL.md", "icon": "🖌️",
        "desc": "从零创建高质量前端界面（Vercel Labs skills）。",
    },
    {
        "id": "web-design-guidelines", "name": "Web 设计规范", "source": "github", "repo": "vercel-labs/skills",
        "file": "web-design-guidelines/SKILL.md", "icon": "📐",
        "desc": "Vercel 的 Web 设计原则：字体、色彩、间距与动效。",
    },
    {
        "id": "brainstorming", "name": "头脑风暴（GitHub）", "source": "github", "repo": "obra/superpowers",
        "file": "skills/brainstorming/SKILL.md", "icon": "🌩️",
        "desc": "结构化发散并收敛创意（obra/superpowers）。",
    },
    {
        "id": "writing-plans", "name": "计划写作", "source": "github", "repo": "obra/superpowers",
        "file": "skills/writing-plans/SKILL.md", "icon": "🗺️",
        "desc": "把任务拆成清晰、可执行的实施计划（obra/superpowers）。",
    },
    {
        "id": "requesting-code-review", "name": "请求代码审查", "source": "github", "repo": "obra/superpowers",
        "file": "skills/requesting-code-review/SKILL.md", "icon": "👀",
        "desc": "组织上下文，让 AI 高效审查你的改动（obra/superpowers）。",
    },
    {
        "id": "subagent-driven-development", "name": "子代理驱动开发", "source": "github", "repo": "obra/superpowers",
        "file": "skills/subagent-driven-development/SKILL.md", "icon": "🤖",
        "desc": "把工作拆给并行子代理执行（obra/superpowers）。",
    },
    {
        "id": "tdd", "name": "测试驱动开发", "source": "github", "repo": "obra/superpowers",
        "file": "skills/test-driven-development/SKILL.md", "icon": "🔴",
        "desc": "红-绿-重构的 TDD 工作流（obra/superpowers）。",
    },
    {
        "id": "code-review",
        "name": "代码审查（内置）",
        "desc": "逐行审查代码：bug、性能、安全、可读性，给出可执行建议。",
        "source": "builtin",
        "icon": "🧪",
        "content": (
            "当用户要求审查代码时：1) 先理解意图；2) 按 bug、安全、性能、可读性分类；"
            "3) 每条问题给出具体位置和修改建议；4) 不确定的地方明确标注。"
        ),
    },
    {
        "id": "explain-code",
        "name": "代码讲解（内置）",
        "desc": "把代码拆成段落讲清楚：数据流、关键决策、易错点。",
        "source": "builtin",
        "icon": "💡",
        "content": (
            "当用户要求讲解代码时：先一句话概括整体作用，再按执行顺序逐段解释；"
            "关键变量和函数调用标出数据流向；最后指出容易踩坑的地方。"
        ),
    },
    {
        "id": "writing-polish", "name": "文字润色（内置）", "source": "builtin", "icon": "✍️",
        "desc": "改写文字：更通顺、更有力、风格可指定。",
        "content": "润色用户文字时：保留原意，改善句式与节奏；按用户指定的正式/口语/简洁风格输出；改动点简要说明。",
    },
    {
        "id": "translation", "name": "精准翻译（内置）", "source": "builtin", "icon": "🌐",
        "desc": "中英互译，术语准确、语气匹配场景。",
        "content": "翻译时先判断原文领域与语气；术语采用该领域惯用译法；无法确定的术语标注说明。",
    },
    {
        "id": "brainstorming", "name": "头脑风暴（内置）", "source": "builtin", "icon": "🧠",
        "desc": "围绕主题发散想法，再收敛成可执行清单。",
        "content": "分两阶段：先不评判地发散 10-20 个想法，再按可行性与价值排序，挑出前 3-5 个展开。",
    },
    {
        "id": "meeting-notes", "name": "会议纪要（内置）", "source": "builtin", "icon": "📝",
        "desc": "把杂乱记录整理成：结论、待办、负责人、截止时间。",
        "content": "整理会议记录时输出：1) 结论 2) 待办（负责人+截止） 3) 未决问题 4) 风险；去掉寒暄和重复。",
    },
    {
        "id": "todo-planner", "name": "任务规划（内置）", "source": "builtin", "icon": "🗂️",
        "desc": "拆目标为可执行步骤，排序、估时、找依赖。",
        "content": "把目标拆成原子任务：每项一个动作、有验收标准；标出依赖关系和里程碑；给出建议顺序。",
    },
    {
        "id": "research", "name": "调研助手（内置）", "source": "builtin", "icon": "🔎",
        "desc": "围绕问题给出信息框架、检索方向与结论模板。",
        "content": "做调研时先拆问题成子问题，列出需要的数据/来源类型，再按重要度给出收集清单和汇总模板。",
    },
    {
        "id": "flashcards", "name": "记忆卡片（内置）", "source": "builtin", "icon": "🃏",
        "desc": "把知识点做成问答卡片，用于复习。",
        "content": "把输入内容转成抽认卡：正面问题、反面答案；每张卡只考一个知识点；按难度分级。",
    },
    {
        "id": "summarizer", "name": "摘要总结（内置）", "source": "builtin", "icon": "📑",
        "desc": "长文压成要点：三句话、要点列表或分节摘要。",
        "content": "总结时先保留核心结论与论据，删除例子和套话；按用户选择输出三句话/要点/分节摘要。",
    },
    {
        "id": "data-analysis", "name": "数据分析（内置）", "source": "builtin", "icon": "📊",
        "desc": "从数据需求到分析方案：指标、口径、可视化建议。",
        "content": "分析数据前先明确业务问题；定义指标口径；给出去噪、分组、趋势与异常检测的方法建议。",
    },
    {
        "id": "excel-formulas", "name": "Excel 公式（内置）", "source": "builtin", "icon": "🧮",
        "desc": "生成可用的 Excel/Sheets 公式并解释。",
        "content": "生成表格公式时注明适用场景、参数含义；优先用通用函数；中文 Excel 与 Google Sheets 差异要说明。",
    },
    {
        "id": "regex-builder", "name": "正则表达式（内置）", "source": "builtin", "icon": "🧩",
        "desc": "根据示例生成并解释正则。",
        "content": "根据正反例生成正则；逐段解释；指出贪婪/回溯等常见坑；给出测试样例。",
    },
    {
        "id": "sql-writer", "name": "SQL 编写（内置）", "source": "builtin", "icon": "🗄️",
        "desc": "写查询、优化、解释执行计划。",
        "content": "写 SQL 时先确认表结构和方言；考虑索引与数据量；大表查询给性能提示；NULL 和类型转换要显式处理。",
    },
    {
        "id": "api-designer", "name": "API 设计（内置）", "source": "builtin", "icon": "🔌",
        "desc": "设计 REST 接口：路径、字段、错误码、幂等。",
        "content": "设计 API 时遵循资源命名、版本化、分页、幂等与错误码规范；给出请求/响应示例。",
    },
    {
        "id": "debugger", "name": "调试专家（内置）", "source": "builtin", "icon": "🐞",
        "desc": "从报错到根因：假设验证、最小复现、修复建议。",
        "content": "调试时先读错误栈定位，形成假设并列出验证步骤；要求最小复现；修复后说明根因。",
    },
    {
        "id": "security-audit", "name": "安全审计（内置）", "source": "builtin", "icon": "🛡️",
        "desc": "检查代码/配置/流程的安全隐患。",
        "content": "按输入校验、注入、越权、密钥管理、依赖漏洞、日志泄露等维度审计；给出风险等级与修复方案。",
    },
    {
        "id": "refactor", "name": "代码重构（内置）", "source": "builtin", "icon": "♻️",
        "desc": "不改变行为的前提下提升结构。",
        "content": "重构前先说明行为保持不变；识别重复、长函数、坏命名、紧耦合；小步给出改法并保留测试。",
    },
    {
        "id": "test-writer", "name": "测试用例（内置）", "source": "builtin", "icon": "✅",
        "desc": "边界、异常、回归用例与测试代码。",
        "content": "根据功能写测试：正常路径、边界值、异常输入、并发/幂等；优先覆盖高风险分支；输出可直接运行的用例。",
    },
    {
        "id": "doc-writer", "name": "文档写作（内置）", "source": "builtin", "icon": "📘",
        "desc": "README、使用手册、接口文档。",
        "content": "写文档先写一句话用途，再按快速开始、核心概念、示例、常见问题组织；示例必须可运行。",
    },
    {
        "id": "changelog", "name": "更新日志（内置）", "source": "builtin", "icon": "📰",
        "desc": "把提交/改动整理成面向用户的 changelog。",
        "content": "按新增/修复/改进/破坏性变更分类；每条面向用户而非实现细节；版本号和日期放标题。",
    },
    {
        "id": "prompt-optimizer", "name": "提示词优化（内置）", "source": "builtin", "icon": "🎛️",
        "desc": "改写提问，补角色、上下文、约束和输出格式。",
        "content": "优化提示词：明确角色与目标、提供上下文、写清约束和输出格式；避免歧义；保留用户原意。",
    },
    {
        "id": "persona-builder", "name": "人设设计（内置）", "source": "builtin", "icon": "🎭",
        "desc": "设计角色：性格、语言风格、边界与示例。",
        "content": "设计人设时给出：身份背景、说话风格、常用句式、禁忌与边界、3 条示例对话。",
    },
    {
        "id": "negotiation", "name": "谈判助手（内置）", "source": "builtin", "icon": "🤝",
        "desc": "梳理筹码、底线、话术与让步顺序。",
        "content": "谈判前先确认目标、底线与替代方案；预演对方立场；给出开场、让步顺序与收尾话术。",
    },
    {
        "id": "learning-planner", "name": "学习规划（内置）", "source": "builtin", "icon": "🎓",
        "desc": "把学科拆成阶段、章节和每日任务。",
        "content": "制定学习计划：先测水平，设定目标与周期；按周拆章节；每阶段留复习与自测；给出验收标准。",
    },
    {
        "id": "fact-check", "name": "事实核查（内置）", "source": "builtin", "icon": "⚖️",
        "desc": "识别可疑断言，给出核查方法与结论置信度。",
        "content": "对陈述逐条标注：可验证/存疑/错误/无法验证；给出信息来源建议；不确定就明说不确定。",
    },
    {
        "id": "math-solver", "name": "数学解题（内置）", "source": "builtin", "icon": "📐",
        "desc": "逐步推导，讲清每一步依据。",
        "content": "解题时先翻译问题，写清每一步使用的定理/公式；给中间结果；最后做合理性检查。",
    },
    {
        "id": "physics-tutor", "name": "物理讲解（内置）", "source": "builtin", "icon": "⚛️",
        "desc": "用直觉和公式两条线讲物理。",
        "content": "先建立物理直觉，再列公式和边界条件；画清受力/流程图；举一个生活例子。",
    },
    {
        "id": "history-guide", "name": "历史梳理（内置）", "source": "builtin", "icon": "📜",
        "desc": "按时间线、因果和多方视角讲历史。",
        "content": "讲历史给出时间线、关键人物、直接与深层原因、结果和影响；区分史实与常见争议解读。",
    },
    {
        "id": "book-summary", "name": "读书笔记（内置）", "source": "builtin", "icon": "📖",
        "desc": "提炼核心观点、案例与可行动启发。",
        "content": "读书笔记包含：一句话主旨、核心观点、关键案例、与自己相关的行动项；保留原文金句。",
    },
    {
        "id": "resume-writer", "name": "简历优化（内置）", "source": "builtin", "icon": "📄",
        "desc": "把经历改写成量化成果与岗位关键词。",
        "content": "改简历用动词开头、量化成果、匹配岗位 JD 关键词；删无关信息；保持一页以内。",
    },
    {
        "id": "email-writer", "name": "邮件写作（内置）", "source": "builtin", "icon": "✉️",
        "desc": "写清楚目的、行动项与语气的邮件。",
        "content": "邮件先写结论，再写背景与行动项；主题可检索；语气按收件人关系调整。",
    },
    {
        "id": "social-copy", "name": "社媒文案（内置）", "source": "builtin", "icon": "📱",
        "desc": "平台适配的短文案：钩子、正文、行动号召。",
        "content": "写社媒文案先给钩子，正文保持短段落；按平台风格调整；结尾给行动号召或互动问题。",
    },
    {
        "id": "seo-copy", "name": "SEO 文案（内置）", "source": "builtin", "icon": "📈",
        "desc": "标题、关键词密度、结构与搜索意图。",
        "content": "写 SEO 文案先确定搜索意图和主关键词；标题含关键词；用 H2/H3 结构化；自然不堆砌。",
    },
    {
        "id": "prd-writer", "name": "PRD 撰写（内置）", "source": "builtin", "icon": "📋",
        "desc": "产品需求：背景、目标、用户故事、验收标准。",
        "content": "写 PRD 包含：背景与目标、用户与场景、功能清单、边界条件、数据埋点、验收标准。",
    },
    {
        "id": "ui-review", "name": "UI 评审（内置）", "source": "builtin", "icon": "🖼️",
        "desc": "从层级、一致性、可访问性角度评审界面。",
        "content": "评审界面按：信息层级、交互一致性、状态覆盖、可访问性、文案与留白逐项给改进建议。",
    },
    {
        "id": "ux-copy", "name": "UX 文案（内置）", "source": "builtin", "icon": "💬",
        "desc": "按钮、空状态、错误提示等界面文案。",
        "content": "写界面文案要具体、简短、可行动；错误提示说明原因和下一步；语气与品牌一致。",
    },
    {
        "id": "decision-matrix", "name": "决策分析（内置）", "source": "builtin", "icon": "🧭",
        "desc": "多选项按权重打分，并指出隐藏假设。",
        "content": "决策时先列标准并赋权，逐项打分；同时列风险和不可逆代价；给结论与备选触发条件。",
    },
    {
        "id": "story-writer", "name": "故事创作（内置）", "source": "builtin", "icon": "📚",
        "desc": "人设、冲突、结构、场景与对话。",
        "content": "写故事先定人物欲望与阻力；按三幕或钩子-冲突-高潮-收尾组织；对话要有潜台词。",
    },
    {
        "id": "speech-writer", "name": "演讲稿（内置）", "source": "builtin", "icon": "🎤",
        "desc": "开场钩子、核心信息、节奏与金句。",
        "content": "演讲稿面向听众：开场 30 秒抓住注意；每段一个核心信息；有故事和数据；结尾可行动。",
    },
    {
        "id": "interview-prep", "name": "面试准备（内置）", "source": "builtin", "icon": "🎯",
        "desc": "岗位拆解、模拟问答、STAR 案例打磨。",
        "content": "按岗位要求列高频问题；用 STAR 法组织经历；预演追问；每次模拟后给评分和改进点。",
    },
    {
        "id": "fitness-plan", "name": "健身计划（内置）", "source": "builtin", "icon": "💪",
        "desc": "按目标、频率、器械制定分阶段训练计划。",
        "content": "先确认目标、经验与伤病；按周排训练日和动作组数；强调渐进负荷、热身与恢复；不含医疗建议。",
    },
]

_MIRRORS = [
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
    "https://gh.llkk.cc/",
    "https://github.moeyy.xyz/",
    "https://ghfast.top/",
    "",
]

_FALLBACKS = {
    "grill-me": _GRILL,
    "skill-creator": (
        "---\nname: skill-creator\ndescription: 指导创建结构化 SKILL.md 技能。\n---\n\n"
        "# skill-creator\n\n创建技能时：写清触发条件、分步骤说明、输入输出格式，并附最少示例。"
    ),
    "pdf": (
        "---\nname: pdf\ndescription: 处理 PDF 文档。\n---\n\n"
        "# pdf\n\n生成 PDF 时先规划页面结构；解析 PDF 时优先提取文本和表格，扫描件提示 OCR 需要。"
    ),
}


def public_catalog(installed_ids: set[str]) -> list[dict]:
    return [
        {
            "id": s["id"], "name": s["name"], "desc": s["desc"],
            "source": s["source"], "icon": s.get("icon", "✨"),
            "repo": s.get("repo", ""), "installed": s["id"] in installed_ids,
        }
        for s in CATALOG
    ]


def get(entry_id: str) -> dict | None:
    return next((s for s in CATALOG if s["id"] == entry_id), None)


def _raw_urls(entry: dict) -> list[str]:
    raw = f"https://raw.githubusercontent.com/{entry['repo']}/main/{entry['file']}"
    return [prefix + raw for prefix in _MIRRORS]


def download_skill(entry: dict) -> tuple[str, bool]:
    """返回 (内容, 是否使用内置兜底)。GitHub 条目按镜像顺序尝试。"""
    if entry.get("source") == "builtin":
        return entry.get("content") or entry.get("desc", ""), False
    last_err: Exception | None = None
    for url in _raw_urls(entry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GiliAgent/0.15"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                text = resp.read().decode("utf-8")
            if (
                text
                and "404: Not Found" not in text
                and "name:" in text.lower()[:2000]
                and not text.startswith("<!")
            ):
                return text, False
        except Exception as e:  # noqa: BLE001
            last_err = e
    fallback = _FALLBACKS.get(entry["id"]) or _generic_fallback(entry)
    if fallback:
        return fallback, True
    raise RuntimeError(f"所有镜像下载失败：{last_err or '未知错误'}")


def _generic_fallback(entry: dict) -> str:
    """未提供专属兜底内容的 GitHub 技能，安装一个精简占位版，保证可安装。"""
    return (
        f"---\nname: {entry['id']}\ndescription: {entry['desc']}\n---\n\n"
        f"# {entry['name']}\n\n{entry['desc']}\n\n"
        "（镜像下载失败，已启用内置精简版；联网后卸载重装可获取完整 SKILL.md。）"
    )


def parse_meta(entry: dict, text: str) -> dict:
    name = entry.get("name", entry["id"])
    desc = entry.get("desc", "")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip() or name
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip() or desc
    return {"name": name, "desc": desc}
