# Sorvia

> **AI 策划 4.0｜跨境电商 Agentic Workflow**  
> From Product Context to Research, Strategy, Content, QA and Planning Output.

---

## 1. 项目简介

Sorvia 是面向跨境电商策划业务的 AI 工作流系统，目标是将传统依赖人工串联的策划流程沉淀为一套**可执行、可复用、可持续迭代**的 Agentic Workflow。

系统主链路为：

```mermaid
flowchart LR
    A[产品资料 / 规格书] --> B[Product Context]
    B --> C[Research]
    C --> D[Strategy]
    D --> E[Content]
    E --> F[QA]
    F --> G[Excel / 策划稿输出]
```
围绕“**产品事实 → 市场研究 → 策略判断 → 内容生成 → 质量校验 → 标准化交付**”建立完整工作链。

---

## 2. 项目目标

### 2.1 核心目标

构建一套适配跨境电商策划业务的 AI 策划系统，实现：

1. **产品信息结构化**  
   从规格书、多 SKU 信息及人工输入中提炼可供后续节点使用的 Product Context。

2. **市场研究自动化**  
   围绕市场 / 类目、关键词、用户意图、竞品、VOC、机会点等维度形成 Research Context。

3. **策划策略结构化**  
   将 Research 与产品事实结合，形成用户需求、竞争机会、购买理由、TOP 卖点及整体沟通策略。

4. **策划内容自动生成**  
   将 Strategy 映射到 F1、F2、BP、A+、Description 等具体策划内容模块。

5. **策划内容质量控制**  
   对产品事实、参数、格式、长度、策略一致性及异常内容进行 QA 检查。

6. **标准化交付**  
   将各节点结果统一沉淀并输出为可直接用于业务的策划稿。

### 2.2 项目最终目标

在 2026/11/30 前完成 **MVP V1**，实现真实 SKU 从输入到最终策划稿输出的稳定闭环，并完成真实品类测试（2个品类）与效果验证。

---

## 3. 项目总计划与版本路线

| 项目阶段 | 计划时间 | 阶段目标 | 对应版本 | 阶段交付 |
| --- | --- | --- | --- | --- |
| （一）调研阶段 | 2026/9/7 - 2026/9/14 | 完成 Skill 调研、明确可借鉴能力、自研范围及整体推进方案 | **v0.1.0-alpha** | 调研结论、技术基线、项目推进计划 |
| （二）系统搭建 | 2026/9/14 - 2026/9/24 | 搭建基础工作流，打通“产品信息输入 → 策划结果输出”完整链路 | **v0.2.0-alpha** | 可运行 Demo |
| （三）策划逻辑 & 内容映射 | 2026/9/28 - 2026/10/23 | 接入 Skill / MCP，完善 Product Context、Research、Strategy，并建立内容映射规则 | **v0.3.0-beta** | 标准化策划输出、完整通用策划稿 |
| （四）效果测试 | 2026/10/26 - 2026/11/30 | 打通工具壁垒，接入现有翻译 / QC 能力，进行真实 SKU 批量测试与优化 | **v1.0.0** | MVP V1、可用策划稿（品类） |

### 当前项目版本

```text
Project Version : v0.1.0-alpha
Project Stage   : （一）调研阶段
Baseline Date   : 2026-09-11
Release Type    : Technical Baseline / Functional Prototype
Runtime         : Windows Local
```

虽然当前代码能力已经提前覆盖部分后续阶段功能，但**版本号仍以项目总计划的阶段验收结果为准**。在阶段验收完成前，不提前提升主版本号。

---

## 4. 当前技术基线（Current Technical Baseline）

截至 2026-09-11，当前系统已经完成端到端主链路的第一版真实实现，并具备真实 SKU 跑通能力。

### 4.1 当前能力状态

| 模块 | 当前状态 | 当前实现说明 | 当前边界 / 后续重点 |
| --- | --- | --- | --- |
| Workflow Engine | ✅ 已实现 | 已建立 Product Context → Research → Strategy → Content → QA → Export 固定工作流，并具备节点状态管理 | 后续完善失败重试、节点级恢复与工程化运行能力 |
| Product Context | ✅ 已实现 V0.1 | 已具备规格书解析、结构化产品参数、SKU / 公共参数 / 差异参数等基础能力 | 多规格书合并、冲突处理、Fact ID、字段规范仍需完善 |
| Research | ✅ 已实现 V0.1 | 当前默认采用 **OpenAI Provider**，通过 OpenAI Responses / Web Research 等通用能力获取泛市场信息 | 专业跨境电商 Skill / MCP 尚未正式接入；专业数据深度仍需增强 |
| Research Evidence | ✅ 已有基础机制 | 已区分产品事实、用户输入、网页证据、模型推理等数据来源，并要求重要结论具备证据意识 | 后续需进一步统一 Evidence Schema、数据可信度和 Provider 优先级 |
| Strategy | ✅ 已实现 V0.1 | 已能够基于 Product Context + Research 形成需求、竞争机会、购买理由、TOP 卖点及沟通策略 | 评分维度、卖点排序、业务规则仍需通过真实 SKU 验证和固化 |
| Content | ✅ 已实现 V0.1 | 已能根据 Strategy 生成结构化策划内容，包括 F1 / F2 / BP / A+ / Description 等模块 | Strategy → Content 映射规则、模块职责、字符规则仍需标准化 |
| QA | ✅ 已实现 V0.1 | 已具备产品参数核查、格式检查、策略一致性等基础 QA 能力 | 后续需接入现有翻译 / QC 能力，并完善自动修复边界 |
| Excel Export | ✅ 已实现 | 已能够将系统结果导出为结构化 Excel / 策划稿 | 后续根据部门正式模板继续冻结字段和版式 |
| Skill / MCP Integration | ⚠️ 未正式接入 | 已完成 Skill 调研与候选池建设 | 当前调研的 Skill 仍处于“调研 / 实测 / 选型”阶段，尚未进入正式工作流 |
| Automated Test | ⚠️ 待建设 | 当前主要依赖人工真实 SKU 跑通验证 | 后续需要建立 Benchmark SKU、回归测试与关键节点测试 |
| Deployment | ⚠️ 本地运行 | 当前主要部署于本地 Windows 环境 | MVP 稳定后再迁移到部门 / 内网服务器或正式运行环境 |

---

## 5. 当前 Research 数据源状态

当前系统 Research 的核心情况如下：

```text
Primary Research Provider : OpenAI
Professional E-commerce Skill Integration : Not Integrated
Current Data Type : General Web / Model-assisted Research
```

### 当前已经调研、但尚未正式接入的能力

- Amazon Sorftime Research MCP Skill
- Amazon Product Research
- Amazon Competitor Analysis
- Amazon Keyword Research
- Product Review Analyze Skill
- MTL Skill
- Amazon Listing Alexa Optimizer

这些 Skill 当前仍属于**候选能力池**。后续必须经过真实调用和统一评测后，再决定：

```text
正式接入 / 借鉴方法论 / 暂不使用
```

项目原则不是“尽可能多地接 Skill”，而是验证：

> **某个 Skill 是否能比当前方案更可靠地解决一个明确业务问题。**

只有通过业务价值、数据质量、稳定性、调用成本和工程接入等维度验证的能力，才进入正式 Workflow。

---

## 6. 当前端到端链路

当前系统已经具备以下完整技术路径：

```text
产品规格书 / SKU 信息
        ↓
Product Context
        ↓
OpenAI Research
        ↓
Research Context / Evidence
        ↓
Strategy Engine
        ↓
TOP Selling Points / Communication Strategy
        ↓
Content Engine
        ↓
F1 / F2 / BP / A+ / Description
        ↓
QA Engine
        ↓
Excel Export / 策划稿
```

当前阶段已经证明：

> **技术链路可以跑通。**

下一阶段需要重点证明：

> **专业数据是否可靠、Strategy 是否正确、Content 是否真正可用于业务。**

---

## 7. 当前已知技术边界

当前版本属于 **Technical Baseline / Functional Prototype**，不是生产级正式版本。

已知边界包括：

1. 当前 Research 主要依赖 OpenAI 泛数据能力，尚未完成专业跨境电商数据源接入。
2. Skill / MCP 已完成调研，但尚未正式进入系统主链路。
3. 多规格书、多 SKU、参数冲突及 Product Fact 管理仍需进一步工程化。
4. Strategy 评分和 TOP 卖点排序逻辑已具备 V0.1，但仍需真实 SKU 批量验证。
5. Strategy → Content 的标准化映射规则仍需进一步固化。
6. QA 已有基础能力，但尚未完成现有智能翻译 / QC 工作台的正式集成。
7. 当前主要依赖本地 Windows 运行环境，正式部署方案尚未冻结。
8. 自动化测试、回归测试、Benchmark 数据集及稳定性监控尚未完整建立。
9. 当前版本重点是验证“业务与技术可行性”，不是一次性完成生产级工程化。

---

## 8. 外部依赖

当前项目存在以下外部能力 / 环境依赖：

- OpenAI API
- 本地 Windows Python 环境
- 现有智能翻译工作台相关能力（Product Context / 后续 QC 集成）
- 后续候选 Skill / MCP / 第三方数据服务

对于后续进入正式 Baseline 的外部依赖，应记录：

```text
依赖名称
依赖版本 / Commit
调用方式
配置项
API / MCP 地址
是否收费
失败降级方案
```

避免出现外部依赖变更后无法复现历史版本的问题。

---

## 9. 版本管理规范

### 9.1 项目版本

项目版本遵循项目总计划阶段：

```text
v0.1.0-alpha   调研阶段 / 技术基线
v0.2.0-alpha   系统搭建 / 可运行 Demo
v0.3.0-beta    策划逻辑与内容映射 / 标准化策划输出
v1.0.0         MVP V1 / 品类可用版本
```

### 9.2 WBS 编号

WBS 是个人开发节奏，例如：

```text
0.1 当前技术状态评估
0.2 基线冻结与版本控制
0.3 建立黄金测试 SKU
...
```

WBS 只表示“任务包和开发顺序”，**不与系统版本号一一对应**。

### 9.3 Git Baseline 建议

当前阶段完成基线冻结后，建议创建 Tag：

```text
v0.1.0-alpha-baseline
```

Tag 用于标记：

> Skill 正式接入前，当前端到端 Functional Prototype 的可回退技术基线。

---

## 10. 当前阶段重点

当前项目处于 **调研阶段收口 + 技术基线确认**。

当前重点不是继续扩展功能，而是：

1. 完成当前系统能力审计并冻结技术基线；
2. 完成 Skill / MCP 实测与能力选型；
3. 明确专业数据源进入 Research 的方式；
4. 保证后续所有开发都围绕：

```text
产品事实
  ↓
真实市场证据
  ↓
用户需求
  ↓
竞争机会
  ↓
TOP卖点
  ↓
内容表达
  ↓
QA验证
  ↓
最终策划稿
```

持续推进。

---

## 11. 项目原则

### 11.1 数据优先于模型想象

产品硬事实、市场数据、竞品信息、VOC 等应优先使用可验证来源。无法取得真实数据时，应明确标记 Missing / Inference，而不是让模型补造。

### 11.2 AI 负责推理，规则负责约束

- AI：研究、归纳、判断、策略、内容生成
- Python / 规则：字段清洗、格式约束、参数核查、字符限制、状态控制、异常处理

### 11.3 Workflow 优先于单点模型效果

项目重点不是某一次 Prompt 是否“写得漂亮”，而是整个链路是否：

- 输入稳定
- 数据可追溯
- 节点职责清晰
- 输出结构稳定
- 结果可验证
- 失败可定位
- 版本可回退

### 11.4 业务验证优先于技术复杂度

在 MVP V1 前，不为了技术先进而引入不必要的复杂架构。所有工程投入都必须服务于真实策划业务价值。

---

## 12. 当前阶段结论

> **Sorvia 当前已经完成 V0.1 技术基线的主要能力建设，端到端 Workflow 已具备真实运行能力。**
>
> 当前 Research Provider 主要采用 OpenAI 泛数据能力，专业跨境电商 Skill / MCP 尚未正式接入。下一阶段的核心不是继续“搭框架”，而是通过 Skill 实测、专业数据接入、业务规则固化和真实 SKU 验证，将 Functional Prototype 推进为可用于业务的 MVP。

---

**Project:** Sorvia  
**Project Plan:** AI 策划 4.0  
**Current Version:** v0.1.0-alpha  
**Current Stage:** 调研阶段 / Technical Baseline  
**Owner:** KK  
**Baseline Date:** 2026-09-11
