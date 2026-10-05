import { uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'

const labels = {
  unknown: ['Proposal evidence unavailable', 'История подготовки изменения недоступна'],
  unfinished: ['Proposal authoring has no recorded completion', 'Подготовка изменения не завершена в журнале'],
  failed: ['Proposal authoring failed', 'Подготовка изменения завершилась ошибкой'],
  abandoned: ['Proposal claim abandoned', 'Подготовка изменения отменена'],
  proposed: ['Proposal authored; checks not recorded here', 'Изменение подготовлено; проверки здесь не записаны'],
  check_unfinished: ['Check has no recorded completion', 'Проверка не имеет записанного завершения'],
  check_abandoned: ['Check claim abandoned', 'Проверка отменена'],
  check_unknown: ['Check evidence unavailable', 'Доказательства проверки недоступны'],
  check_failed: ['Recorded checks failed', 'Записанные проверки не пройдены'],
  check_passed: ['Recorded checks passed; fresh evidence required', 'Записанные проверки пройдены; нужны актуальные доказательства'],
  advanced: ['Recorded run base updated', 'Исходная база запуска обновлена в журнале'],
}

export default function UpstreamRecovery({ proposal, ru }) {
  useUILanguage()

  if (!proposal) return null
  return <div className="base-revision" aria-label={((ru ? 'Последний перенос кода' : uiText('Latest code reuse proposal')))}>
    <h4>{((ru ? 'Последний перенос кода' : uiText('Latest code reuse proposal')))}</h4>
    <p><strong>{(labels[proposal.status] || labels.unknown)[ru ? 1 : 0]}</strong></p>
    {proposal.source_node_id != null && <p>{((ru ? 'Из эксперимента' : uiText('From experiment')))} #{proposal.source_node_id}</p>}
    <p>{((ru ? 'Последние 200 событий — только история: без разрешения на повтор или перенос. Работа автора не измеряется. Перед продолжением попросите Assistant прочитать исходный запрос и актуальные доказательства.' : uiText('Last 200 events only: no retry/advance authority or writer liveness. Ask Assistant to read the original request and current evidence before continuing.')))}</p>
    {proposal.status === 'advanced' && <p>{((ru ? 'Изменена база этого запуска. Это не merge или push в пользовательский репозиторий.' : uiText('This updates the run base. It is not a merge or push to the user repository.')))}</p>}
    {proposal.proposal_id && <details><summary>{((ru ? 'Исходные ID и hash запроса' : uiText('Original request identity')))}</summary>
      <p>{uiText("proposal_id: ")}<code>{proposal.proposal_id}</code></p>
      <p>{uiText("expected_request_hash: ")}<code className="base-digest">{proposal.request_hash}</code></p>
      <p>{((ru ? 'Сохранность файла нужно проверить через upstream_request. Один указатель не доказывает наличие тела.' : uiText('Verify retained bytes with upstream_request. A pointer alone does not prove the body is available.')))}</p>
    </details>}
  </div>
}
