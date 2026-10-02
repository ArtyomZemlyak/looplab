// Presentation only: changing language must never change the provider operation or its fence.
const ru = {
  'Checking previous result…': 'Читаем предыдущий результат…',
  'Testing active LLM…': 'Проверяем связь…',
  'Reloading settings…': 'Обновляем настройки…',
  'Reload saved settings': 'Обновить настройки',
  'Outcome unresolved': 'Исход неизвестен',
  'Check previous result': 'Проверить предыдущий результат',
  'Test active LLM': 'Проверить связь',
  'Start new check (may bill)': 'Новая проверка (может оплачиваться)',
  'Dismiss warning': 'Признать неизвестный исход',
  'Reload only · no provider request': 'Только чтение настроек · без запроса к модели',
  'Replay only · no new provider request': 'Чтение прежнего результата · без нового запроса к модели',
  'A new check requires confirmation and may bill again': 'Новая проверка требует подтверждения и может оплачиваться повторно',
  'Provider check in progress and may be billed': 'Запрос выполняется и может оплачиваться',
  'Provider test blocked by credential state': 'Сначала проверьте доступ к модели в Settings',
  'One provider request may be billed': 'Один запрос к модели может оплачиваться',
  'Previous result unavailable': 'Предыдущий результат недоступен',
  'Provider outcome unresolved': 'Исход запроса к модели неизвестен',
  'Previous result pending': 'Предыдущий результат ещё не подтверждён',
  'Another check is running': 'Уже выполняется другая проверка',
  'Previous LLM responded': 'Модель из прежних настроек ответила',
  'Active LLM responded': 'Связь подтверждена',
  'Check not started': 'Проверка не началась',
  'Previous LLM failed': 'Проверка прежней модели не прошла',
  'Active LLM failed': 'Связь не подтверждена',
  'Requires confirmation because this creates a new provider operation that may be billed.': 'Новый запрос может оплачиваться, поэтому требуется подтверждение.',
  'Acknowledge the unknown outcome and remove its recovery gate without contacting the provider.': 'Признать неизвестный исход и снять блокировку восстановления. Запрос к модели не отправляется.',
  'The previous provider outcome is unresolved and may already be billed. Start a new active LLM check that may bill again?': 'Исход предыдущего запроса неизвестен, и он мог быть оплачен. Начать новую проверку, которая может оплачиваться повторно?',
  'Acknowledge and dismiss this unresolved provider outcome? A later Test active LLM action will create a new provider operation and may bill again.': 'Признать неизвестный исход и снять блокировку? Это не подтверждает связь. Следующая проверка создаст новый запрос, который может оплачиваться повторно.',
}

const healthText = text => ru[text] || text

function healthRecoveryHelp(status) {
  if (status.configurationChanged) return 'Настройки изменились. Обновите их перед новой проверкой.'
  if (status.terminalUnknown) return 'Исход предыдущего запроса неизвестен, он мог быть оплачен. Новая проверка — отдельное действие с подтверждением; она может оплачиваться повторно.'
  if (status.reconcilable) return 'Подтверждённого ответа пока нет. Проверьте предыдущий результат: это читает сохранённый ответ и не отправляет новый запрос к модели.'
  if (status.anotherCheckBusy) return 'Дождитесь завершения другой проверки. Этот запрос не обращался к модели.'
  if (status.errorKind === 'credentials') return 'Провайдер отклонил доступ. Проверьте API key и его привязку к адресу в Settings, сохраните изменения и явно проверьте связь снова.'
  if (status.errorKind === 'rate_limit') return 'Провайдер отклонил запрос из-за лимита. Проверьте квоту или дождитесь её восстановления перед новой проверкой.'
  return status.error
}

export const ruHealthCopy = {
  text: healthText,
  recoveryHelp: healthRecoveryHelp,
  unloadedTitle: 'Сначала прочитайте сохранённые настройки.',
  draftNote: count => `Несохранённых изменений исключено: ${count}`,
  previousConfiguration: 'Результат относится к прежним настройкам. Модель из текущих настроек не проверялась.',
}
