const $ = (selector) => document.querySelector(selector);
const form = $('#extract-form');
const urlInput = $('#youtube-url');
const speakerInput = $('#speaker');
const sessionInput = $('#session-number');
const typeInput = $('#transcript-type');
const countInput = $('#question-count');
const bankInput = $('#bank-name');
const prefixInput = $('#prefix');
const status = $('#status');
const transcriptPanel = $('#transcript-panel');
const reviewPanel = $('#review-panel');
const languagePicker = $('#language-picker');
const languageSelect = $('#language-select');
const selectedLanguage = $('#selected-language');
const videoId = $('#video-id');
const transcript = $('#transcript');
const questionList = $('#question-list');
const approvalDialog = $('#approval-dialog');

const SECTIONS = [
  'Science and Technology', 'Mathematics', 'Filipino Communication',
  'English Communication', 'Philippine History', 'Life and Works of Rizal',
  'The Contemporary World', 'Art Appreciation', 'Ethics', 'Understanding the Self',
  'Learners and Learning Principles', 'Curriculum, Methods, and Technology',
  'Teaching Profession', 'Assessment of Learning', 'Field Study and Action Research',
];

let lastExtraction = null;
let questions = [];
let topicPool = [];
let validation = null;
let aiResults = [];
let bankEdited = false;
let prefixEdited = false;

function setStatus(message, kind = '') {
  status.textContent = message;
  status.className = kind;
}

function errorMessage(error) {
  const detail = error?.detail ?? error?.message ?? 'Something went wrong.';
  if (typeof detail === 'string') return detail;
  return detail.message || JSON.stringify(detail);
}

async function api(path, payload, options = {}) {
  const response = await fetch(path, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (options.blob && response.ok) return { response, data: await response.blob() };
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw data;
  return data;
}

function timestamp(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const total = Math.floor(seconds);
  return `[${Math.floor(total / 60).toString().padStart(2, '0')}:${(total % 60).toString().padStart(2, '0')}]`;
}

function linesToText(lines) {
  return lines.map((line) => `${timestamp(line.start)} ${line.text}`.trim()).join('\n');
}

function suggestedBank() {
  const speaker = speakerInput.value.trim() || 'Melvin';
  const session = sessionInput.value.trim();
  return session ? `${speaker} Session ${session} - ${typeInput.value}` : '';
}

function suggestedPrefix() {
  const session = sessionInput.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return session ? `melvin-s${session}` : '';
}

function updateSuggestions() {
  if (!bankEdited) bankInput.value = suggestedBank();
  if (!prefixEdited) prefixInput.value = suggestedPrefix();
}

[speakerInput, sessionInput, typeInput].forEach((input) => input.addEventListener('input', () => { updateSuggestions(); invalidateValidation(); }));
bankInput.addEventListener('input', () => { bankEdited = true; invalidateValidation(); });
prefixInput.addEventListener('input', () => { prefixEdited = true; invalidateValidation(); });
updateSuggestions();

function sessionPayload() {
  return {
    speaker: speakerInput.value.trim(),
    session_number: sessionInput.value.trim(),
    transcript_type: typeInput.value,
    bank_name: bankInput.value.trim(),
    prefix: prefixInput.value.trim(),
    transcript: transcript.value,
  };
}

function draftPayload() {
  return { ...sessionPayload(), questions, topic_pool: topicPool };
}

function requireSessionAndTranscript() {
  const payload = sessionPayload();
  if (!payload.speaker || !payload.session_number || !payload.bank_name || !payload.prefix) throw new Error('Complete all session fields first.');
  if (!payload.transcript.trim()) throw new Error('Extract or paste a transcript first.');
  return payload;
}

async function checkOllama() {
  const pill = $('#ollama-status');
  pill.textContent = 'Checking Ollama...';
  pill.className = 'status-pill neutral';
  try {
    const data = await api('/api/ollama-status?model=qwen3%3A8b');
    if (!data.installed) {
      pill.textContent = 'qwen3:8b not installed';
      pill.className = 'status-pill warning';
      return false;
    }
    pill.textContent = 'Ollama · qwen3:8b ready';
    pill.className = 'status-pill success';
    return true;
  } catch (error) {
    pill.textContent = 'Ollama unavailable';
    pill.className = 'status-pill error';
    return false;
  }
}

async function extract(languageCode = null) {
  setStatus('Fetching available captions...');
  try {
    const data = await api('/api/extract', { url: urlInput.value.trim(), language_code: languageCode });
    lastExtraction = data;
    transcript.value = linesToText(data.lines);
    selectedLanguage.textContent = data.selected_language.name;
    videoId.textContent = `${data.video_id} · ${data.lines.length} lines`;
    languageSelect.innerHTML = data.languages.map((language) => `<option value="${escapeHtml(language.code)}">${escapeHtml(language.name)}${language.is_generated ? ' (auto-generated)' : ''}</option>`).join('');
    languageSelect.value = data.selected_language.code;
    languagePicker.hidden = data.languages.length < 2;
    transcriptPanel.hidden = false;
    $('#transcript-dirty').hidden = true;
    setStatus('Transcript loaded. Review and edit it before generating.', 'success-text');
  } catch (error) {
    setStatus(errorMessage(error), 'error-text');
  }
}

form.addEventListener('submit', (event) => { event.preventDefault(); extract(); });
languageSelect.addEventListener('change', () => extract(languageSelect.value));
transcript.addEventListener('input', () => { $('#transcript-dirty').hidden = false; invalidateValidation(); });

$('#copy-transcript').addEventListener('click', async () => {
  await navigator.clipboard.writeText(transcript.value);
  setStatus('Edited transcript copied.', 'success-text');
});

async function saveTranscript(overwrite = false) {
  const payload = requireSessionAndTranscript();
  const filename = `session-${sessionInput.value.trim().replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-|-$/g, '')}-raw.txt`;
  try {
    const data = await api('/api/save-transcript', { session_number: payload.session_number, transcript_type: payload.transcript_type, transcript: payload.transcript, filename, overwrite });
    $('#transcript-dirty').hidden = true;
    setStatus(`Edited transcript saved to ${data.path}.`, 'success-text');
  } catch (error) {
    if (!overwrite && errorMessage(error).includes('already exists')) {
      if (window.confirm(`${errorMessage(error)}\n\nReplace the existing transcript?`)) return saveTranscript(true);
      setStatus('Save cancelled. The existing transcript was not changed.');
      return;
    }
    setStatus(errorMessage(error), 'error-text');
  }
}

$('#save-transcript').addEventListener('click', () => saveTranscript());

async function generateQuestions(extra = {}) {
  let payload;
  try { payload = requireSessionAndTranscript(); } catch (error) { setStatus(error.message, 'error-text'); return; }
  const button = $('#generate-button');
  button.disabled = true;
  setStatus(`Generating ${countInput.value} questions with qwen3:8b. This may take several minutes...`);
  try {
    const data = await api('/api/generate', { ...payload, question_count: Number(countInput.value), model: 'qwen3:8b', accept_fewer: false, allow_topic_reuse: false, ...extra });
    questions = data.questions;
    topicPool = data.topic_pool || [];
    validation = data.validation;
    aiResults = [];
    reviewPanel.hidden = false;
    $('#draft-meta').textContent = `${questions.length} draft questions · ${data.topic_pool?.length || 0} grounded topics · ${data.draft}`;
    renderQuestions();
    showValidation(data.validation);
    reviewPanel.scrollIntoView({ behavior: 'smooth' });
    setStatus('Draft generated. Review every question before approval.', 'success-text');
  } catch (error) {
    const detail = error?.detail;
    if (detail?.code === 'INSUFFICIENT_UNIQUE_TOPICS') {
      if (window.confirm(`${detail.message}\n\nGenerate only ${detail.unique_topic_count} questions?`)) return generateQuestions({ accept_fewer: true });
      if (window.confirm('Permit additional questions from already-used topics? Conceptual duplicates will still be flagged for review.')) return generateQuestions({ allow_topic_reuse: true });
    }
    setStatus(errorMessage(error), 'error-text');
  } finally {
    button.disabled = false;
    checkOllama();
  }
}

$('#generate-button').addEventListener('click', () => generateQuestions());

function normalizedItem(item) {
  const safe = item && !Array.isArray(item) ? item : {};
  const options = Array.isArray(safe.options) ? safe.options : [];
  return {
    section: safe.section ?? '', topic_key: safe.topic_key ?? '', question: safe.question ?? '',
    options: [0, 1, 2, 3].map((i) => options[i] ?? ''),
    correct_answer: Number.isInteger(safe.correct_answer) ? safe.correct_answer : 0,
    explanation: safe.explanation ?? '', source_evidence: safe.source_evidence ?? '',
    validation_override: Boolean(safe.validation_override),
  };
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function renderQuestions() {
  questionList.innerHTML = questions.map((raw, index) => {
    const item = normalizedItem(raw);
    const ai = aiResults.find((result) => result.index === index);
    return `<article class="question-card" data-index="${index}">
      <div class="question-top"><div><span class="number">Question ${index + 1}</span>${ai ? `<span class="ai-result ${ai.status.toLowerCase()}">${escapeHtml(ai.status)}</span>` : ''}${item.validation_override ? '<span class="ai-result warning">OVERRIDDEN</span>' : ''}</div><div class="question-actions"><button type="button" class="text-button regenerate">Regenerate Question</button><button type="button" class="text-button delete">Delete Question</button></div></div>
      ${ai ? validationDetails(ai) : ''}
      <div class="editor-grid"><label>Section<select data-field="section">${sectionOptions(item.section)}</select></label><label>Topic Key<input data-field="topic_key" value="${escapeHtml(item.topic_key)}"></label><label>Correct Answer<select data-field="correct_answer">${['A', 'B', 'C', 'D'].map((letter, answer) => `<option value="${answer}" ${item.correct_answer === answer ? 'selected' : ''}>${letter}</option>`).join('')}</select></label></div>
      <label>Question<textarea class="question-input" data-field="question">${escapeHtml(item.question)}</textarea></label>
      <div class="option-grid">${item.options.map((option, optionIndex) => `<label><span class="option-letter">${String.fromCharCode(65 + optionIndex)}</span><input data-field="option" data-option="${optionIndex}" value="${escapeHtml(option)}"></label>`).join('')}</div>
      <label>Explanation<textarea class="explanation-input" data-field="explanation">${escapeHtml(item.explanation)}</textarea></label>
      <label>Source Evidence<textarea class="evidence-input" data-field="source_evidence">${escapeHtml(item.source_evidence)}</textarea></label>
      ${ai && !ai.section_correct && ai.suggested_section ? `<button type="button" class="secondary apply-section" data-section="${escapeHtml(ai.suggested_section)}">Apply Suggested Section: ${escapeHtml(ai.suggested_section)}</button>` : ''}
      ${ai?.status === 'FLAGGED' ? '<button type="button" class="secondary accept-anyway">Accept Anyway</button>' : ''}
      <div class="question-errors" hidden></div>
    </article>`;
  }).join('');
  questionList.querySelectorAll('.question-card').forEach(wireQuestionCard);
}

function validationDetails(result) {
  const badges = [
    ['SOURCE', result.source_supported], ['ANSWER', result.answer_supported],
    ['EXPLANATION', result.explanation_supported && !result.unsupported_expansion],
    ['SECTION', result.section_correct], ['UNIQUENESS', result.unique_concept],
    ['OPTIONS', result.single_correct_answer],
  ];
  const issues = (result.issues || []).map((issue) => `<div>${escapeHtml(issue)}</div>`).join('');
  return `<div class="validation-details"><div class="dimension-badges">${badges.map(([name, passed]) => `<span class="dimension ${passed ? 'pass' : 'flagged'}">${name}: ${passed ? 'PASS' : 'FLAGGED'}</span>`).join('')}</div>${issues ? `<div class="validation-issues">${issues}</div>` : ''}</div>`;
}

function refreshApprovalState() {
  const unresolvedFlag = aiResults.some((result) => result.status === 'FLAGGED' && !questions[result.index]?.validation_override);
  $('#approve-button').disabled = !validation?.valid || unresolvedFlag;
}

function sectionOptions(current) {
  const values = SECTIONS.includes(current) ? SECTIONS : [current, ...SECTIONS].filter(Boolean);
  return values.map((section) => `<option ${section === current ? 'selected' : ''}>${escapeHtml(section)}</option>`).join('');
}

function wireQuestionCard(card) {
  const index = Number(card.dataset.index);
  card.querySelectorAll('[data-field]').forEach((control) => control.addEventListener('input', () => {
    const item = normalizedItem(questions[index]);
    const field = control.dataset.field;
    if (field === 'section') item.section = control.value;
    if (field === 'topic_key') item.topic_key = control.value;
    if (field === 'question') item.question = control.value;
    if (field === 'option') item.options[Number(control.dataset.option)] = control.value;
    if (field === 'correct_answer') item.correct_answer = Number(control.value);
    if (field === 'explanation') item.explanation = control.value;
    if (field === 'source_evidence') item.source_evidence = control.value;
    item.validation_override = false;
    questions[index] = item;
    invalidateValidation();
  }));
  card.querySelector('.delete').addEventListener('click', () => {
    questions.splice(index, 1);
    invalidateValidation();
    renderQuestions();
  });
  card.querySelector('.regenerate').addEventListener('click', () => regenerateOne(index));
  card.querySelector('.apply-section')?.addEventListener('click', (event) => {
    questions[index] = { ...normalizedItem(questions[index]), section: event.currentTarget.dataset.section, validation_override: false };
    invalidateValidation(); renderQuestions();
  });
  card.querySelector('.accept-anyway')?.addEventListener('click', () => {
    questions[index] = { ...normalizedItem(questions[index]), validation_override: true };
    const result = aiResults.find((entry) => entry.index === index);
    if (result) result.overridden = true;
    renderQuestions(); refreshApprovalState();
  });
}

function invalidateValidation() {
  validation = null;
  aiResults = [];
  document.querySelectorAll('.ai-result').forEach((node) => node.remove());
  $('#validation-badge').textContent = 'Not validated';
  $('#validation-badge').className = 'status-pill neutral';
  $('#validate-ai').disabled = true;
  $('#download-js').disabled = true;
  $('#approve-button').disabled = true;
  $('#general-errors').hidden = true;
  document.querySelectorAll('.question-errors').forEach((node) => { node.hidden = true; node.innerHTML = ''; });
}

async function regenerateOne(index) {
  const card = questionList.querySelector(`[data-index="${index}"]`);
  const button = card.querySelector('.regenerate');
  button.disabled = true;
  button.textContent = 'Regenerating...';
  try {
    const data = await api('/api/regenerate-question', { ...draftPayload(), question_index: index, model: 'qwen3:8b' });
    questions[index] = data.question;
    invalidateValidation();
    renderQuestions();
    setStatus(`Question ${index + 1} regenerated and draft saved.`, 'success-text');
  } catch (error) {
    setStatus(errorMessage(error), 'error-text');
    button.disabled = false;
    button.textContent = 'Regenerate Question';
  }
}

$('#regenerate-all').addEventListener('click', () => {
  if (!window.confirm('Replace the entire reviewed draft? Your current question edits will be lost.')) return;
  generateQuestions();
});

$('#save-draft').addEventListener('click', async () => {
  try {
    const data = await api('/api/save-draft', draftPayload());
    setStatus(`Draft saved to ${data.draft}.`, 'success-text');
  } catch (error) { setStatus(errorMessage(error), 'error-text'); }
});

$('#validate-all').addEventListener('click', async () => {
  try {
    const data = await api('/api/validate', draftPayload());
    validation = data;
    aiResults = [];
    showValidation(data);
    setStatus(data.valid ? `All deterministic checks passed. Draft saved to ${data.draft}.` : 'Validation found issues. Review the highlighted questions.', data.valid ? 'success-text' : 'error-text');
  } catch (error) { setStatus(errorMessage(error), 'error-text'); }
});

function showValidation(report) {
  document.querySelectorAll('.question-errors').forEach((node) => { node.hidden = true; node.innerHTML = ''; });
  const general = $('#general-errors');
  general.hidden = !report.general_errors?.length;
  general.innerHTML = (report.general_errors || []).map((message) => `<div>${escapeHtml(message)}</div>`).join('');
  (report.errors || []).forEach((error) => {
    const node = questionList.querySelector(`[data-index="${error.index}"] .question-errors`);
    if (node) { node.hidden = false; node.insertAdjacentHTML('beforeend', `<div>${escapeHtml(error.message)}</div>`); }
  });
  const badge = $('#validation-badge');
  badge.textContent = report.valid ? 'Validation passed' : `${(report.errors?.length || 0) + (report.general_errors?.length || 0)} issue(s)`;
  badge.className = `status-pill ${report.valid ? 'success' : 'error'}`;
  $('#validate-ai').disabled = !report.valid;
  $('#download-js').disabled = !report.valid;
  $('#approve-button').disabled = !report.valid;
}

$('#validate-ai').addEventListener('click', async () => {
  const button = $('#validate-ai');
  button.disabled = true;
  button.textContent = 'Checking sources...';
  setStatus('Ollama is checking every question against the edited transcript...');
  try {
    const data = await api('/api/validate-ai', { transcript: transcript.value, transcript_type: typeInput.value, questions, model: 'qwen3:8b' });
    aiResults = data.results;
    renderQuestions();
    const flagged = aiResults.filter((item) => item.status === 'FLAGGED').length;
    refreshApprovalState();
    setStatus(flagged ? `${flagged} question(s) flagged. Edit or regenerate them; no questions were rewritten.` : 'All questions passed AI source validation.', flagged ? 'error-text' : 'success-text');
  } catch (error) { setStatus(errorMessage(error), 'error-text'); }
  finally { button.disabled = !validation?.valid; button.textContent = 'Validate with AI'; }
});

$('#download-js').addEventListener('click', async () => {
  try {
    const { response, data } = await api('/api/download-js', draftPayload(), { blob: true });
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || `melvin-session-${sessionInput.value.trim()}-questions.js`;
    const url = URL.createObjectURL(data);
    const link = document.createElement('a');
    link.href = url; link.download = filename; link.click();
    URL.revokeObjectURL(url);
    setStatus(`Standalone JavaScript saved and downloaded as ${filename}. The reviewer was not modified.`, 'success-text');
  } catch (error) { setStatus(errorMessage(error), 'error-text'); }
});

$('#approve-button').addEventListener('click', () => {
  if (!validation?.valid) return;
  $('#confirm-bank').textContent = bankInput.value.trim();
  $('#confirm-prefix').textContent = prefixInput.value.trim();
  $('#confirm-count').textContent = String(questions.length);
  approvalDialog.showModal();
});

$('#confirm-add').addEventListener('click', async () => {
  const button = $('#confirm-add');
  button.disabled = true;
  button.textContent = 'Adding safely...';
  try {
    const data = await api('/api/approve', { ...draftPayload(), confirmed: true });
    approvalDialog.close();
    $('#approve-button').disabled = true;
    setStatus(`${data.message} Bank: ${data.bank}. Questions added: ${data.questions_added}. Backup: ${data.backup}. Reviewer bank: ${data.reviewer_bank}.`, 'success-text');
  } catch (error) {
    setStatus(errorMessage(error), 'error-text');
  } finally {
    button.disabled = false;
    button.textContent = 'Confirm and Add';
  }
});

checkOllama();
