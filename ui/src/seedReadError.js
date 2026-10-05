import { uiText } from './uiLanguage.js'
// Named read failures offer different recovery; never echo arbitrary server/parser text.
export function seedReadError(error, ru) {
  if (error?.name === 'TimeoutError') return (ru ? 'Сервер не ответил вовремя. Проверьте подключение и повторите чтение.' : uiText('The server did not respond in time. Check the connection and retry the read.'))
  if (['node_attempt_changed', 'run_generation_changed', 'run_generation_conflict', 'generation_conflict'].includes(error?.code)) return (ru ? 'Опыт или поколение рана изменились. Обновите состояние, затем откройте файлы текущей попытки.' : uiText('The experiment or run generation changed. Refresh state, then open files for the current attempt.'))
  if (error?.code === 'seed_archive_unavailable') return (ru ? 'Записанный архив или его квитанция недоступны. Проверьте сохранённый архив и журнал событий; восстановите исходные данные перед повторным чтением.' : uiText('The recorded archive or its evidence is unavailable. Inspect the saved archive and event log; restore the original source before retrying.'))
  if (error?.code === 'seed_page_invalid') return (ru ? 'Ответ не прошёл проверку целостности или принадлежности опыту. Обновите состояние и повторите чтение.' : uiText('The response failed integrity or experiment identity checks. Refresh state and retry the read.'))
  return (ru ? 'Не удалось прочитать файлы базы. Проверьте подключение и состояние рана, затем повторите чтение.' : uiText('Base files could not be read. Check the connection and run state, then retry the read.'))
}
