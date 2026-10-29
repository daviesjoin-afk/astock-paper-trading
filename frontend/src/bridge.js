/* PR-55：inline 事件处理器的**兼容桥**。
   index.html 与各模块生成的 HTML 里大量使用 onclick="fn(...)"；
   模块化后这些名字不再自动是全局，这里逐个显式挂到 window 上。
   新增 inline handler 时，把被调用的函数加到对应 import 与赋值列表。
   由 backend/test_frontend_module_contract.py 校验覆盖率。 */

import { adaptiveEsc, adaptiveJsArg } from "./core/format.js";
import { compareAdaptiveValidationRun, openAdaptiveValidationRun, refreshAdaptiveRobustness, refreshAdaptiveValidationRuns, selectAdaptiveRobustnessRun, submitAdaptiveRobustness, submitAdaptiveValidation } from "./features/adaptive.js";
import { activatePage, refreshApp, showPaperWorkspace, toggleDark } from "./core/navigation.js";
import { applyAdaptiveRiskCandidate, applyAdaptiveSelectionCandidate, approveAdaptiveNeural, loadAdaptive, loadEvolutionStatus, openAdaptiveResearchRun, recordAdaptiveFeedback, refreshAdaptiveResearchHistory, refreshAdaptiveTimelineNow, retryAdaptiveAiWindow, rollbackAdaptiveRebalance, rollbackAdaptiveRisk, rollbackAdaptiveSelection, runAdaptive, runAdaptiveAdvisor, runAdaptiveAiTuning, runAdaptiveResearchSuite, runAdaptiveResearchTask, runDualAiTuning, runNewsLearning, setAdaptiveSection, switchToAdaptiveAI, triggerEvolution } from "./features/adaptive.js";
import { loadPaperExecution, verifyExecutionOrder } from "./features/execution.js";
import { clearPaperOrderPreview, filterPaperHistoryRows, loadPaperStockHistory, loadPortfolioWorkspace, pausePaper, previewPaperOrder, resetPaper, resumePaper, runPaperNow, selectPaperHistoryQuick, setPaperOrderSide, setPaperTerminalFilter, startPaper, submitPaperOrder, syncPaperCapitalHint, togglePaperLimitPrice } from "./features/paper.js";
import { applyPaperRiskAuditFilter, refreshPaperRisk } from "./features/risk.js";
import { cancelManualDataUpdate, choosePaperStrategy, chooseStrategy, filterSelectionEvaluationRows, loadDataValidity, loadPaperSelection, loadSelectionEvaluation, refreshSelectionEvaluation, runPaperSelection, showSelectionValidation, startFactorIncrementalUpdate, startManualDataUpdate } from "./features/selection.js";

window.activatePage = activatePage;
window.adaptiveEsc = adaptiveEsc;
window.adaptiveJsArg = adaptiveJsArg;
window.applyAdaptiveRiskCandidate = applyAdaptiveRiskCandidate;
window.applyAdaptiveSelectionCandidate = applyAdaptiveSelectionCandidate;
window.applyPaperRiskAuditFilter = applyPaperRiskAuditFilter;
window.approveAdaptiveNeural = approveAdaptiveNeural;
window.cancelManualDataUpdate = cancelManualDataUpdate;
window.choosePaperStrategy = choosePaperStrategy;
window.chooseStrategy = chooseStrategy;
window.clearPaperOrderPreview = clearPaperOrderPreview;
window.filterPaperHistoryRows = filterPaperHistoryRows;
window.filterSelectionEvaluationRows = filterSelectionEvaluationRows;
window.loadAdaptive = loadAdaptive;
window.loadDataValidity = loadDataValidity;
window.loadEvolutionStatus = loadEvolutionStatus;
window.loadPaperExecution = loadPaperExecution;
window.loadPortfolioWorkspace = loadPortfolioWorkspace;
window.loadPaperSelection = loadPaperSelection;
window.loadPaperStockHistory = loadPaperStockHistory;
window.loadSelectionEvaluation = loadSelectionEvaluation;
window.openAdaptiveResearchRun = openAdaptiveResearchRun;
window.openAdaptiveValidationRun = openAdaptiveValidationRun;
window.compareAdaptiveValidationRun = compareAdaptiveValidationRun;
window.refreshAdaptiveValidationRuns = refreshAdaptiveValidationRuns;
window.refreshAdaptiveRobustness = refreshAdaptiveRobustness;
window.selectAdaptiveRobustnessRun = selectAdaptiveRobustnessRun;
window.submitAdaptiveRobustness = submitAdaptiveRobustness;
window.submitAdaptiveValidation = submitAdaptiveValidation;
window.pausePaper = pausePaper;
window.previewPaperOrder = previewPaperOrder;
window.recordAdaptiveFeedback = recordAdaptiveFeedback;
window.refreshAdaptiveTimelineNow = refreshAdaptiveTimelineNow;
window.refreshAdaptiveResearchHistory = refreshAdaptiveResearchHistory;
window.refreshApp = refreshApp;
window.refreshPaperRisk = refreshPaperRisk;
window.refreshSelectionEvaluation = refreshSelectionEvaluation;
window.resetPaper = resetPaper;
window.resumePaper = resumePaper;
window.retryAdaptiveAiWindow = retryAdaptiveAiWindow;
window.rollbackAdaptiveRebalance = rollbackAdaptiveRebalance;
window.rollbackAdaptiveRisk = rollbackAdaptiveRisk;
window.rollbackAdaptiveSelection = rollbackAdaptiveSelection;
window.runAdaptive = runAdaptive;
window.runAdaptiveAdvisor = runAdaptiveAdvisor;
window.runAdaptiveAiTuning = runAdaptiveAiTuning;
window.runAdaptiveResearchSuite = runAdaptiveResearchSuite;
window.runAdaptiveResearchTask = runAdaptiveResearchTask;
window.runDualAiTuning = runDualAiTuning;
window.runNewsLearning = runNewsLearning;
window.runPaperNow = runPaperNow;
window.runPaperSelection = runPaperSelection;
window.selectPaperHistoryQuick = selectPaperHistoryQuick;
window.setAdaptiveSection = setAdaptiveSection;
window.setPaperOrderSide = setPaperOrderSide;
window.setPaperTerminalFilter = setPaperTerminalFilter;
window.showPaperWorkspace = showPaperWorkspace;
window.showSelectionValidation = showSelectionValidation;
window.startFactorIncrementalUpdate = startFactorIncrementalUpdate;
window.startManualDataUpdate = startManualDataUpdate;
window.startPaper = startPaper;
window.submitPaperOrder = submitPaperOrder;
window.switchToAdaptiveAI = switchToAdaptiveAI;
window.syncPaperCapitalHint = syncPaperCapitalHint;
window.toggleDark = toggleDark;
window.togglePaperLimitPrice = togglePaperLimitPrice;
window.triggerEvolution = triggerEvolution;
window.verifyExecutionOrder = verifyExecutionOrder;

