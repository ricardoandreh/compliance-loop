# Operação

O laço tem alarmes para as falhas que **não** viram escalação — o laço parado,
não o laço trabalhando: detecção perdida no EventBridge, expiração de exceção
não reavaliada, falha por registro na sincronização de exceções, registro de
stream abandonado, queda de Lambda, timeout e throttling do Step Functions.
Cada disparo vira uma **issue no GitHub** (rótulo `operations-alarm`), aberta
pela mesma Lambda das escalações: um canal só para tudo o que exige uma pessoa.
A volta a OK comenta na issue, e quem a fecha é quem seguiu o runbook. O
caminho passa por uma fila, então uma queda do GitHub atrasa a notificação em
vez de perdê-la. Os alarmes aparecem também no painel `compliance-loop-dev-loop`,
que é o que resta se o próprio canal do GitHub cair (token expirado, por
exemplo): o tópico tem uma assinatura só, por decisão. O runbook, com o que cada alarme significa e a
primeira ação, está no `design.md` (*Operação: alarmes e runbook*).

Nada que o laço deixa de processar some. Os consumidores dos streams do
DynamoDB têm tentativas limitadas e uma fila de falhas. Uma detecção que o
EventBridge não consegue entregar à máquina de estados (por exemplo, a role
perdeu a permissão) vai para a fila `intake-failures`, e
`scripts/replay_intake.py` inicia depois a execução que ela deveria ter
iniciado — no máximo uma por evento. Os logs das Lambdas têm retenção de 30
dias; o `violations_log` e a tabela de exceções têm recuperação pontual; e a
trilha do CloudTrail tem validação de integridade, para que a evidência
independente usada na H2 seja à prova de adulteração — `validate-logs`: 11 de
11 arquivos válidos desde que foi ligada, depois da série final.

As duas falhas silenciosas mais graves foram provocadas na conta real
(`scripts/ops_fault_injection.py`, resultado em
`results/ops_fault_injection.json`): uma exceção que a sincronização não
consegue agendar e uma detecção recusada por falta de permissão. Nos dois casos
o alarme disparou — o primeiro em ~1 min, o da fila em ~3 min, tempos ditados
pelo período das métricas — e abriu a sua issue no GitHub no mesmo segundo; a
volta a OK virou comentário nela. A detecção recusada ficou na fila até ser
reprocessada, avaliando o bucket como a original teria avaliado.

**Reconciliação periódica (opcional).** O laço é orientado a eventos: o que o
CloudTrail não entrega não é avaliado. Uma varredura periódica é configuração,
não mudança de modelo: um agendamento no EventBridge Scheduler que inicia a
máquina de estados para cada recurso com a entrada de um `CreateBucket` (ou
`CreateVolume`), que já avalia todos os controles daquele recurso — ou, para
controles de conta, com o `control_id`, como a expiração de uma exceção já faz.
Não está ativada; desenho e custo (~US$ 0,0005 por recurso por varredura) no
`design.md`, L15.
