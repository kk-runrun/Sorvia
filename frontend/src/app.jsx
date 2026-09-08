const {
  Alert,
  Button,
  Card,
  Divider,
  Input,
  Message,
  Modal,
  Space,
  Spin,
  Tag,
} = arco;

const { useEffect, useMemo, useRef, useState } = React;
const TextArea = Input.TextArea;

const STATUS_META = {
  pending: { label: '待执行', color: 'gray' },
  running: { label: '执行中', color: 'arcoblue' },
  completed: { label: '已完成', color: 'green' },
  failed: { label: '失败', color: 'red' },
  waiting_confirmation: { label: '等待用户确认', color: 'orange' },
};

const SUPPORTED_SPEC_EXTENSIONS = ['.xlsx', '.xlsm', '.xls'];

function formatSize(size) {
  if (!size) return '0 KB';
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(data?.detail || data || '请求失败');
  }
  return data;
}

function isSupportedSpecFile(file) {
  const name = String(file?.name || '').toLowerCase();
  return SUPPORTED_SPEC_EXTENSIONS.some((ext) => name.endsWith(ext));
}

function createUploadItem(file, relativePath = '') {
  const displayName = relativePath || file.webkitRelativePath || file.name;
  return {
    id: `${displayName}-${file.size}-${file.lastModified}`,
    file,
    displayName,
    size: file.size,
    lastModified: file.lastModified,
  };
}

function readFileEntry(entry, pathPrefix = '') {
  return new Promise((resolve) => {
    entry.file(
      (file) => resolve([createUploadItem(file, `${pathPrefix}${file.name}`)]),
      () => resolve([])
    );
  });
}

function readDirectoryEntries(reader) {
  return new Promise((resolve) => {
    const entries = [];
    function readBatch() {
      reader.readEntries((batch) => {
        if (!batch.length) {
          resolve(entries);
          return;
        }
        entries.push(...batch);
        readBatch();
      }, () => resolve(entries));
    }
    readBatch();
  });
}

async function readEntry(entry, pathPrefix = '') {
  if (!entry) return [];
  if (entry.isFile) {
    return readFileEntry(entry, pathPrefix);
  }
  if (entry.isDirectory) {
    const reader = entry.createReader();
    const children = await readDirectoryEntries(reader);
    const nested = await Promise.all(children.map((child) => readEntry(child, `${pathPrefix}${entry.name}/`)));
    return nested.flat();
  }
  return [];
}

async function collectDroppedFiles(dataTransfer) {
  const items = Array.from(dataTransfer.items || []);
  if (items.length) {
    const collected = [];
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if (entry) {
        const nested = await readEntry(entry);
        collected.push(...nested);
      } else {
        const file = item.getAsFile();
        if (file) collected.push(createUploadItem(file));
      }
    }
    return collected;
  }
  return Array.from(dataTransfer.files || []).map((file) => createUploadItem(file));
}

function Shell({ children }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">SP</div>
          <div>
            <div className="brand-title">超级策划</div>
            <div className="brand-subtitle">输入产品基础信息，自动完成策划稿制作流程，输出策划稿。</div>
          </div>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  );
}

function HomePage({ onCreated }) {
  const [keywords, setKeywords] = useState('');
  const [overview, setOverview] = useState('');
  const [files, setFiles] = useState([]);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef(null);

  function appendUploadItems(items) {
    if (!items.length) return;
    const supportedItems = items.filter((item) => isSupportedSpecFile(item.file));
    const skippedCount = items.length - supportedItems.length;
    if (skippedCount > 0) {
      Message.info(`已跳过 ${skippedCount} 个非 Excel 规格书文件`);
    }
    if (!supportedItems.length) return;
    setFiles((current) => {
      const existing = new Set(current.map((item) => item.id));
      const nextItems = supportedItems.filter((item) => !existing.has(item.id));
      if (!nextItems.length) {
        Message.info('文件已在上传列表中');
        return current;
      }
      Message.success(`已添加 ${nextItems.length} 个文件`);
      return [...current, ...nextItems];
    });
  }

  function addFiles(event) {
    const incoming = Array.from(event.target.files || []).map((file) => createUploadItem(file));
    appendUploadItems(incoming);
    setFileInputKey((value) => value + 1);
  }

  async function handleDrop(event) {
    event.preventDefault();
    event.stopPropagation();
    setDragActive(false);
    const dropped = await collectDroppedFiles(event.dataTransfer);
    appendUploadItems(dropped);
  }

  function handleDragOver(event) {
    event.preventDefault();
    event.stopPropagation();
    setDragActive(true);
  }

  function handleDragLeave(event) {
    event.preventDefault();
    event.stopPropagation();
    if (!event.currentTarget.contains(event.relatedTarget)) {
      setDragActive(false);
    }
  }

  function removeFile(index) {
    setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
  }

  function clearForm() {
    setKeywords('');
    setOverview('');
    setFiles([]);
    setFileInputKey((value) => value + 1);
    Message.success('已清空当前页面内容');
  }

  async function submit() {
    if (!keywords.trim()) {
      Message.warning('请输入产品关键词');
      return;
    }

    const form = new FormData();
    form.append('keywords', keywords.trim());
    form.append('overview', overview.trim());
    files.forEach((item) => form.append('spec_files', item.file, item.displayName));

    setSubmitting(true);
    try {
      const project = await api('/api/projects', { method: 'POST', body: form });
      Message.success('已创建策划任务');
      onCreated(project);
    } catch (error) {
      Message.error(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Shell>
      <section className="page-head">
        <div>
          <h1>超级策划</h1>
          <p>输入产品基础信息，自动完成策划稿制作流程，输出策划稿。</p>
        </div>
      </section>

      <Card className="panel input-panel" bordered={false}>
        <div className="form-grid">
          <label className="field full">
            <span className="field-label">产品关键词（中/英）</span>
            <Input
              size="large"
              value={keywords}
              onChange={setKeywords}
              placeholder="请输入产品核心关键词，支持中文 / 英文，例如：背包帐篷 / Backpacking Tent"
              allowClear
            />
          </label>

          <label className="field full">
            <span className="field-label">产品概述</span>
            <TextArea
              value={overview}
              onChange={setOverview}
              autoSize={{ minRows: 7, maxRows: 12 }}
              placeholder="可填写产品用途、核心功能、目标用户、主要使用场景、核心优势、与竞品的差异、目标市场等。信息越完整，策划结果越准确。"
            />
          </label>

          <div className="field full">
            <span className="field-label">规格书上传</span>
            <div
              className={`upload-zone ${dragActive ? 'drag-active' : ''}`}
              onClick={() => inputRef.current?.click()}
              onDrop={handleDrop}
              onDragEnter={handleDragOver}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
            >
              <input
                key={fileInputKey}
                ref={inputRef}
                type="file"
                multiple
                accept=".xlsx,.xlsm,.xls"
                className="file-input"
                onChange={addFiles}
              />
              <div className="upload-title">选择规格书文件，或拖拽文件 / 文件夹到此处</div>
              <div className="upload-hint">支持上传多份 Excel 规格书；拖入文件夹时会递归读取其中的 Excel 文件；单份规格书可包含一个或多个 SKU。</div>
            </div>

            {files.length > 0 && (
              <div className="file-list">
                {files.map((item, index) => (
                  <div className="file-row" key={`${item.id}-${index}`}>
                    <div>
                      <div className="file-name">{item.displayName}</div>
                      <div className="file-meta">{formatSize(item.size)}</div>
                    </div>
                    <Button size="small" type="secondary" onClick={() => removeFile(index)}>删除</Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <Divider />

        <div className="action-row">
          <Button type="primary" size="large" loading={submitting} onClick={submit}>提交</Button>
          <Button size="large" disabled={submitting} onClick={clearForm}>清空</Button>
        </div>
      </Card>
    </Shell>
  );
}

function StepTimeline({ project }) {
  const steps = project?.workflow_state?.steps || [];
  return (
    <div className="step-list">
      {steps.map((step, index) => {
        const meta = STATUS_META[step.status] || STATUS_META.pending;
        return (
          <div className={`step-row ${step.status}`} key={step.id}>
            <div className="step-index">{index + 1}</div>
            <div className="step-main">
              <div className="step-title-line">
                <div>
                  <div className="step-name">{step.name}</div>
                  <div className="step-layer">{step.layer}</div>
                </div>
                <Tag color={meta.color}>{meta.label}</Tag>
              </div>
              {step.summary && <div className="step-summary">{step.summary}</div>}
              {step.error_message && (
                <Alert
                  className="step-alert"
                  type="error"
                  content={`${step.error_type === 'system' ? '系统问题' : '执行失败'}：${step.error_message} 请联系 IT 人员解决。`}
                />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ConfirmationModal({ project, onSubmitted }) {
  const request = project?.workflow_state?.confirmation_request;
  const [answers, setAnswers] = useState({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const initial = {};
    (request?.questions || []).forEach((question) => {
      initial[question.id] = question.answer || '';
    });
    setAnswers(initial);
  }, [request?.id]);

  async function submit() {
    const hasAnswer = Object.values(answers).some((value) => String(value || '').trim());
    if (!hasAnswer) {
      Message.warning('请至少填写一个确认答案');
      return;
    }
    setLoading(true);
    try {
      const updated = await api(`/api/projects/${project.project_id}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers }),
      });
      Message.success('已提交确认，Workflow 将继续执行');
      onSubmitted(updated);
    } catch (error) {
      Message.error(error.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Modal
      visible={Boolean(request)}
      title={request?.title || '等待用户确认'}
      okText="提交并继续"
      cancelText="暂不处理"
      confirmLoading={loading}
      onOk={submit}
      onCancel={() => {}}
      maskClosable={false}
      className="confirm-modal"
    >
      {request && (
        <div>
          <Alert type="warning" content={request.reason} />
          <div className="question-list">
            {request.questions.map((question) => (
              <label className="question-item" key={question.id}>
                <span className="field-label">{question.prompt}</span>
                {question.background && <span className="question-bg">{question.background}</span>}
                <TextArea
                  value={answers[question.id] || ''}
                  onChange={(value) => setAnswers((current) => ({ ...current, [question.id]: value }))}
                  autoSize={{ minRows: 3, maxRows: 6 }}
                  placeholder="请输入你的判断或补充资料"
                />
              </label>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
}

function TaskPage({ initialProject, onNewProject }) {
  const [project, setProject] = useState(initialProject);
  const [loading, setLoading] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);

  const completedCount = useMemo(() => {
    return (project?.workflow_state?.steps || []).filter((step) => step.status === 'completed').length;
  }, [project]);

  useEffect(() => {
    setProject(initialProject);
  }, [initialProject?.project_id]);

  useEffect(() => {
    if (!project?.project_id) return undefined;
    const terminal = ['completed', 'failed', 'paused'].includes(project.workflow_state.status);
    if (terminal) return undefined;

    const timer = window.setInterval(async () => {
      try {
        const data = await api(`/api/projects/${project.project_id}`);
        setProject(data);
      } catch (error) {
        Message.error(error.message);
      }
    }, 1000);

    return () => window.clearInterval(timer);
  }, [project?.project_id, project?.workflow_state?.status]);

  async function refresh() {
    setLoading(true);
    try {
      setProject(await api(`/api/projects/${project.project_id}`));
    } catch (error) {
      Message.error(error.message);
    } finally {
      setLoading(false);
    }
  }

  function downloadExport() {
    if (!project?.project_id) return;
    window.open(`/api/projects/${project.project_id}/download`, '_blank');
  }

  const state = project?.workflow_state?.status || 'running';
  const projectName = project?.project_name || project?.user_input?.keywords || '未命名策划项目';
  const parsedSpec = project?.parsed_spec_result || {};
  const planningContext = project?.planning_context || {};
  const researchResult = project?.research_result || {};
  const strategyResult = project?.strategy_result || {};
  const contentResult = project?.content_result || {};
  const qaResult = project?.qa_result || {};
  const exportResult = project?.export_result || {};
  const isWaitingConfirmation = state === 'paused' && Boolean(project?.workflow_state?.confirmation_request);
  const stateLabel = state === 'completed'
    ? '已完成'
    : state === 'failed'
      ? '失败'
      : isWaitingConfirmation
        ? '等待用户确认'
        : state === 'paused'
          ? '等待验收'
          : '执行中';
  const debugPayload = {
    planning_context: {
      status: planningContext.status,
      user_input: planningContext.user_input,
      spec_result: planningContext.spec_result,
      sku_list: planningContext.sku_list,
      product_common_parameters: planningContext.product_common_parameters,
      sku_group_common_parameters: planningContext.sku_group_common_parameters,
      sku_differential_parameters: planningContext.sku_differential_parameters,
      research_brief: planningContext.research_brief,
      human_notes: planningContext.human_notes,
    },
    research_result: {
      status: researchResult.status,
      provider: researchResult.research_provider,
      provider_status: researchResult.provider_status,
      target_market: researchResult.target_market,
      web_search_enabled: researchResult.web_search_enabled,
      web_search_used: researchResult.web_search_used,
      generated_at: researchResult.generated_at,
      data_quality: researchResult.data_quality,
      market_context: researchResult.market_context,
      keyword_context: researchResult.keyword_context,
      competitor_context: researchResult.competitor_context,
      voc_context: researchResult.voc_context,
      market_tier: researchResult.market_tier,
      opportunity_signals: researchResult.opportunity_signals,
      evidence_sample: (researchResult.evidence || []).slice(0, 12),
    },
    strategy_result: {
      status: strategyResult.status,
      strategy_version: strategyResult.strategy_version,
      generated_at: strategyResult.generated_at,
      summary: strategyResult.summary,
      product_category: strategyResult.product_category,
      strategy_category: strategyResult.strategy_category,
      user_mindset: strategyResult.user_mindset,
      target_audience: strategyResult.target_audience,
      main_uses: strategyResult.main_uses,
      main_scenario: strategyResult.main_scenario,
      secondary_scenario: strategyResult.secondary_scenario,
      user_needs_summary: strategyResult.user_needs_summary,
      focus_points: strategyResult.focus_points,
      pain_points: strategyResult.pain_points,
      purchase_barriers: strategyResult.purchase_barriers,
      competitive_landscape: strategyResult.competitive_landscape,
      competitive_opportunity: strategyResult.competitive_opportunity,
      competitive_strategy: strategyResult.competitive_strategy,
      user_purchase_reason: strategyResult.user_purchase_reason,
      communication_strategy: strategyResult.communication_strategy,
      selling_point_ranking: strategyResult.selling_point_ranking,
      confidence: strategyResult.confidence,
      needs_verification: strategyResult.needs_verification,
      evidence_sample: (strategyResult.evidence || []).slice(0, 12),
    },
    content_result: {
      status: contentResult.status,
      content_version: contentResult.content_version,
      generated_at: contentResult.generated_at,
      generation_provider: contentResult.generation_provider,
      generation_model: contentResult.generation_model,
      generation_api: contentResult.generation_api,
      format_rules_version: contentResult.format_rules_version,
      summary: contentResult.summary,
      content_mapping: contentResult.content_mapping,
      modules: contentResult.modules,
      content_items: contentResult.content_items,
      confidence: contentResult.confidence,
      needs_verification: contentResult.needs_verification,
      length_rewrite_summary: contentResult.length_rewrite_summary,
      minimal_checks: contentResult.minimal_checks,
      evidence_sample: (contentResult.evidence || []).slice(0, 12),
    },
    qa_result: {
      status: qaResult.status,
      qa_version: qaResult.qa_version,
      generated_at: qaResult.generated_at,
      summary: qaResult.summary,
      checked_items: qaResult.checked_items,
      passed_items: qaResult.passed_items,
      corrected_items: qaResult.corrected_items,
      overall_risk: qaResult.overall_risk,
      data_quality: qaResult.data_quality,
      unresolved_issues: qaResult.unresolved_issues,
      correction_history: qaResult.correction_history,
      parameter_issues: qaResult.parameter_issues,
      forbidden_word_issues: qaResult.forbidden_word_issues,
      translation_issues: qaResult.translation_issues,
      format_issues: qaResult.format_issues,
      strategy_issues: qaResult.strategy_issues,
    },
    export_result: {
      status: exportResult.status,
      export_version: exportResult.export_version,
      generated_at: exportResult.generated_at,
      summary: exportResult.summary,
      artifact_filename: exportResult.artifact_filename,
      artifact_type: exportResult.artifact_type,
      template_used: exportResult.template_used,
      template_path: exportResult.template_path,
      sheet_names: exportResult.sheet_names,
      download_url: exportResult.download_url,
      qa_unresolved_count: exportResult.qa_unresolved_count,
    },
    standard_chinese_parameter_table_sample: (parsedSpec.standard_chinese_parameter_table || []).slice(0, 30),
  };

  return (
    <Shell>
      <section className="page-head task-head">
        <div>
          <h1>任务执行</h1>
          <p>按固定Workflow执行。</p>
        </div>
        <Space>
          <Button onClick={refresh} loading={loading}>刷新</Button>
          <Button type="secondary" onClick={onNewProject}>新建任务</Button>
        </Space>
      </section>

      <div className="task-layout">
        <Card className="panel task-card" bordered={false}>
          <div className="task-summary">
            <div>
              <div className="summary-label">Planning Project</div>
              <div className="summary-id">{projectName}</div>
            </div>
            <Tag color={state === 'completed' ? 'green' : state === 'failed' ? 'red' : state === 'paused' ? 'orange' : 'arcoblue'}>
              {stateLabel}
            </Tag>
          </div>
          <div className="progress-line">
            <div style={{ width: `${Math.round((completedCount / 6) * 100)}%` }} />
          </div>
          <StepTimeline project={project} />
        </Card>

        <aside className="side-panel">
          <Card className="panel" bordered={false}>
            <div className="summary-label">User Input</div>
            <div className="side-block">
              <b>项目名称</b>
              <p>{projectName}</p>
            </div>
            <div className="side-block">
              <b>产品关键词</b>
              <p>{project.user_input.keywords}</p>
            </div>
            <div className="side-block">
              <b>产品概述</b>
              <p>{project.user_input.overview || '未填写'}</p>
            </div>
            <div className="side-block">
              <b>规格书</b>
              <p>{project.spec_files.length ? `${project.spec_files.length} 份文件` : '未上传'}</p>
            </div>
            <div className="side-block">
              <b>解析结果</b>
              <p>
                {parsedSpec.status === 'completed'
                  ? `规格书解析完成，识别 ${parsedSpec.sku_count} 个 SKU`
                  : parsedSpec.summary || '等待产品资料解析'}
              </p>
            </div>
            {parsedSpec.sku_list?.length > 0 && (
              <div className="sku-mini-list">
                {parsedSpec.sku_list.slice(0, 6).map((sku) => (
                  <div className="sku-mini-row" key={sku.sku_id}>
                    <span>{sku.sequence}</span>
                    <b>{sku.sku}</b>
                  </div>
                ))}
                {parsedSpec.sku_list.length > 6 && <div className="sku-more">+{parsedSpec.sku_list.length - 6} 个 SKU</div>}
              </div>
            )}
            {exportResult?.status === 'completed' && exportResult?.artifact_filename && (
              <div className="side-block">
                <b>导出文件名</b>
                <p>{exportResult.artifact_filename}</p>
                <Button long type="primary" onClick={downloadExport}>下载策划稿</Button>
              </div>
            )}
            <Button long type="outline" onClick={() => setDebugOpen((value) => !value)}>
              {debugOpen ? '收起调试查看' : '调试查看'}
            </Button>
          </Card>

          {state === 'running' && (
            <Card className="panel small-card" bordered={false}>
              <Spin size={18} />
              <span>{(project.workflow_state.steps || []).find((step) => step.id === project.workflow_state.current_step_id)?.summary || 'Workflow 正在执行当前步骤'}</span>
            </Card>
          )}
        </aside>
      </div>

      {debugOpen && (
        <Card className="panel debug-panel" bordered={false}>
          <div className="debug-head">
            <div>
              <div className="summary-label">Debug View</div>
              <h2>Planning Context / 标准中文参数表</h2>
            </div>
            <Tag color="arcoblue">开发调试</Tag>
          </div>
          <pre className="debug-json">{JSON.stringify(debugPayload, null, 2)}</pre>
        </Card>
      )}

      <ConfirmationModal project={project} onSubmitted={setProject} />
    </Shell>
  );
}

function App() {
  const [project, setProject] = useState(null);

  if (project) {
    return <TaskPage initialProject={project} onNewProject={() => setProject(null)} />;
  }
  return <HomePage onCreated={setProject} />;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
