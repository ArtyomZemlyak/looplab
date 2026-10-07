import { uiText } from './uiLanguage.js'
// Named read failures offer different recovery; never echo arbitrary server/parser text.
export function seedReadError(error, ru) {
  if (error?.name === 'TimeoutError') return (ru ? 'Сервер не ответил вовремя. Проверьте подключение и повторите чтение.' : uiText('The server did not respond in time. Check the connection and retry the read.'))
  if (['node_attempt_changed', 'run_generation_changed', 'run_generation_conflict', 'generation_conflict'].includes(error?.code)) return (ru ? 'Опыт или поколение рана изменились. Обновите состояние, затем откройте файлы текущей попытки.' : uiText('The experiment or run generation changed. Refresh state, then open files for the current attempt.'))
  if (error?.code === 'seed_archive_unavailable') return (ru ? 'Записанный архив или его квитанция недоступны. Проверьте сохранённый архив и журнал событий; восстановите исходные данные перед повторным чтением.' : uiText('The recorded archive or its evidence is unavailable. Inspect the saved archive and event log; restore the original source before retrying.'))
  // Not a read failure: the file arrived, but this browser cannot hash it (`crypto.subtle` exists
  // only in a secure context — HTTPS or localhost), so its text is withheld rather than shown
  // unverified. Telling the operator to "check the connection" would send them the wrong way.
  if (error?.code === 'seed_hash_unavailable') return (ru ? 'Этот браузер не может проверить содержимое файла: SHA-256 доступен только в защищённом контексте (HTTPS или localhost). Файл не показан без проверки. Откройте интерфейс по HTTPS или через localhost.' : uiText('This browser cannot verify file contents: SHA-256 is only available in a secure context (HTTPS or localhost). The file is not shown unverified. Open the UI over HTTPS or on localhost.'))
  if (error?.code === 'seed_page_invalid') return (ru ? 'Ответ не прошёл проверку целостности или принадлежности опыту. Обновите состояние и повторите чтение.' : uiText('The response failed integrity or experiment identity checks. Refresh state and retry the read.'))
  return (ru ? 'Не удалось прочитать файлы базы. Проверьте подключение и состояние рана, затем повторите чтение.' : uiText('Base files could not be read. Check the connection and run state, then retry the read.'))
}

// The digest a recorded UTF-8 file is held to. `crypto.subtle` is undefined on a non-secure origin;
// that is a named refusal (`seed_hash_unavailable`), never the generic connection failure a bare
// TypeError would have read as.
export async function seedTextSha256(text, source = globalThis.crypto) {
  if (typeof source?.subtle?.digest !== 'function') throw { code: 'seed_hash_unavailable' }
  let digest
  try { digest = await source.subtle.digest('SHA-256', new TextEncoder().encode(text)) }
  catch { throw { code: 'seed_hash_unavailable' } }
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}
