# 超级策划

本项目是「超级策划」本地 Demo。当前已完成 Task 1 工程骨架，并在 Task 2 接入真实规格书拆解能力，用于建立后续 Research、Strategy、Content、QA 共用的 Planning Context。

## 启动方式

```bash
cd D:\策划Skill\超级策划
python -m pip install -r requirements.txt
python app.py
```

启动后终端会打印本地访问地址，默认从 `http://127.0.0.1:8601` 开始查找可用端口。

## 当前真实能力

- 首页输入产品关键词、产品概述。
- 支持选择多份规格书，也支持拖拽 Excel 文件或文件夹上传；拖入文件夹时会递归收集其中的 Excel 文件。
- 提交后创建 Planning Project，并将上传文件保存到 `data/uploads/{project_id}`。
- 后端保存项目 JSON 到 `data/projects/{project_id}.json`。
- 系统内部保留唯一 `project_id`；普通任务页展示 `project_name`，默认来自用户提交的产品关键词。
- 任务页按固定六步展示 Workflow 状态。
- Workflow 支持 `待执行 / 执行中 / 已完成 / 失败 / 等待用户确认`。
- Product Context 阶段复用 `D:\智能翻译工作台1.2\spec_sheet_arranger` 的规格书整理 pipeline。
- 单份规格书可真实生成标准中文参数表，识别单 SKU / 多 SKU。
- 已建立 Planning Context：用户输入、规格书文件、中文标准参数表、SKU 列表、公共参数、SKU Group 参数、单 SKU 差异参数、Research Brief。
- 业务资料不足或硬参数冲突会暂停 Workflow 并弹出人工确认，一个问题对应一个输入框。
- 系统异常会将当前步骤标记为失败，并提示联系 IT 人员处理。

## 当前模拟能力

- Research、策略策划、文案生成、QA 质检、策划稿生成仍为模拟执行。
- Research 暂未接入 Sorftime 或 Research Skill。
- Strategy Prompt、Listing 文案、最终 QA、Excel 策划稿输出均未实现。

## 架构说明

```text
超级策划/
├─ app.py
├─ requirements.txt
├─ frontend/
│  ├─ index.html
│  └─ src/
│     ├─ app.jsx
│     └─ styles.css
├─ super_planner/
│  ├─ api/
│  │  └─ routes.py
│  ├─ core/
│  │  ├─ config.py
│  │  └─ exceptions.py
│  ├─ schemas/
│  │  ├─ project.py
│  │  └─ workflow.py
│  ├─ services/
│  │  ├─ spec_sheet_adapter.py
│  │  ├─ planning_context_builder.py
│  │  ├─ product_context.py
│  │  ├─ research.py
│  │  ├─ strategy.py
│  │  ├─ content.py
│  │  ├─ qa.py
│  │  ├─ export.py
│  │  └─ openai_client.py
│  ├─ storage/
│  │  ├─ files.py
│  │  └─ repository.py
│  └─ workflow/
│     └─ engine.py
└─ data/
   ├─ projects/
   ├─ uploads/
   └─ outputs/
```

## 人工确认模拟

如果不上传规格书，Workflow 会在「产品资料解析」暂停并要求确认是否允许暂不使用规格书继续。

如果产品概述中的硬参数与规格书拆解结果冲突，Workflow 会暂停并要求确认正确参数或 SKU 适用范围。提交回答后会从当前位置继续执行。

如需模拟系统问题，可在关键词中输入 `系统错误` 或 `system-error`，Workflow 会在「市场研究」步骤失败，并提示联系 IT 人员解决。

## Task 2 规格书原则

- 后续涉及产品事实和参数时，以 `parsed_spec_result.standard_chinese_parameter_table` 为核心事实依据。
- Content 后续不得自行补充、猜测或改写具体参数；必须从规格书拆解结果取值。
- QA 后续也应使用同一份参数结果反查最终文案。
- V0.1 保留多规格书上传结构，但只解析上传顺序中的第一份规格书；复杂多文件合并后续加强。
- 导出文件名预留规则：`超级策划_{项目名称}_{YYYYMMDD}.xlsx`，例如 `超级策划_背包帐篷_Backpacking_Tent_20260907.xlsx`。
