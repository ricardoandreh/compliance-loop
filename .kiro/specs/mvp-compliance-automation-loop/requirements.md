# Documento de Requisitos

## Introdução

O MVP Compliance Automation Loop é um sistema de automação de conformidade baseado no padrão MAPE-K (Monitor, Analyze, Plan, Execute, Knowledge) que detecta desvios de conformidade em recursos cloud, classifica o estado do recurso, e executa ações corretivas automaticamente ou escala para operadores humanos quando necessário.

O sistema monitora eventos de mutação em recursos AWS via CloudTrail, avalia conformidade usando políticas Rego executadas pelo Open Policy Agent (OPA), e remedia violações usando OpenTofu. O MVP implementa controle para bloqueio de acesso público em buckets S3.

> **Escopo construído.** O texto acima e os requisitos 1–19 são a especificação inicial, preservada. O sistema construído implementa seis controles em dois serviços (S3 e EC2), verifica a convergência de cada remediação numa segunda rodada, escala por cinco motivos (não só falha de remediação), remedia com uma autoridade restrita a cada incidente e tem uma camada de operação. O que mudou em relação aos requisitos 1–19 está em *Revisões dos requisitos*; o que foi acrescentado sem estar previsto está em *Requisitos acrescentados durante o desenvolvimento* (20–27).

**Forma dos critérios.** Os critérios de aceitação seguem o padrão EARS (*Easy Approach to Requirements Syntax*), com as palavras-chave em maiúsculas: **QUANDO** (gatilho), **SE … ENTÃO** (situação indesejada), **DEVE** e **NÃO DEVE** (obrigação); um critério sem gatilho vale sempre. Os critérios 1–19 foram escritos originalmente em inglês e traduzidos sem mudança de conteúdo. Nomes de componentes (Monitor, Analyze, Plan, Execute, Orchestrator), de campos e de estados ficam como no código.

## Revisões dos requisitos

Este documento é a especificação inicial do MVP e fica preservado como tal. O sistema construído a partir dele evoluiu, e alguns critérios foram alterados por decisão de projeto — cada um está marcado abaixo com *(revisado)*, *(parcialmente atendido)* ou *(não atendido)* e justificado no `design.md`. A situação de cada requisito e de cada propriedade de corretude, com a evidência correspondente, está em [`traceability.md`](traceability.md).

| Critério | Especificado | Construído | Justificativa |
|---|---|---|---|
| 1.1 | captura em até 5 s, só para `PutBucketPublicAccessBlock` | 7,2–8,1 s em média por série; 5 de 180 amostras (nove séries) abaixo de 5 s | latência de entrega do CloudTrail, fora do sistema — *não atendido* |
| 1.6, 10.1 | apenas `ctrl-s3-public-block` | seis controles em dois serviços | extensibilidade: controles cadastrados em tabela (*Arquitetura de Extensibilidade*) |
| 2.5 | encerrar após a remediação | agenda uma rodada de verificação antes de encerrar | fechamento explícito do laço (*Fechamento do laço em duas rodadas*) |
| 3.2 | política `s3_public_access` | uma política Rego por controle | idem 1.6 |
| 3.6, 5.3 | índice por `control_id` | índice `resource-control-index` | consulta pontual por recurso e controle, sem varredura |
| 4.2–4.4 | política no pacote da Lambda | política no S3, versionada por `policy_version` | mudar política sem novo deploy da Lambda |
| 5.2 | `exception_id` UUID | identificador de texto escolhido por quem registra; o sistema não gera nem valida o formato | o registro de exceções pressupõe uma ferramenta de gestão (aprovação, geração do identificador, validação), que é engenharia de plataforma e fica fora do escopo — *limitação L20* |
| 6.1 | o Analyze grava a linha | o orquestrador grava; falha de avaliação não gera linha | Analyze sem efeitos colaterais; modelo de três estados |
| 8.1, 19.1 | escalar quando o Execute falha | escalar por cinco motivos: remediação falha, não convergência, reincidência (disjuntor), controle detectivo e falha do sistema | os requisitos 20–22 criaram caminhos de escalação sem Execute |
| 8.6 | timestamp da violação | instante em que a escalação foi decidida (a avaliação fica na linha do log, ligada pelo `violation_id`) | a mensagem descreve a escalação; a violação já tem registro próprio |
| 8.8 | URL da execução | ARN da execução (`step_functions_execution_arn`), que a busca do console abre | o ARN é estável e não depende da região do console |
| 9.4 | `/tmp/remediation.tfstate` | `/tmp/<control_id>/<recurso>.tfstate`, num diretório apagado a cada invocação | um estado que sobrevivesse de uma invocação anterior, com `-refresh=false`, faria o OpenTofu responder *No changes* a um recurso violado de novo e pular a remediação |
| 11.1 | detecção em até 10 s, com o bucket criado já público | 164 de 180 amostras (91%), pior caso 12,08 s; o cenário cria o bucket e em seguida remove o bloqueio | idem 1.1 — *parcialmente atendido*. Desde 2023 o S3 cria buckets com o bloqueio de acesso público ativo, então o cenário precisa removê-lo |
| 15.3, 15.7 | apenas `bucketName` | também volume e recurso de conta | controles de EC2 |
| 15.6 | descartar evento incompleto | identificador sintético da conta | o EventBridge só admite eventos de controles cadastrados (L3) |
| 16.2 | nenhum estado externo além do Knowledge_Base | também a configuração do recurso (3.1) e a política no S3 (4.2 revisado) | o critério contradizia o 3.1; o que vale é que a classificação depende só dessas entradas, lidas uma vez por avaliação |
| 17.1–17.3 | 15 / 45 / 60 s | 60 / 90 / 600 s | os valores originais não cobriam o pior caso (*Propriedade 28*) |
| 17.4 | registrar o timeout e encerrar | timeout do Analyze ou do Execute é escalado; timeout da execução inteira dispara um alarme | encerrar em silêncio deixaria o recurso sem dono |
| 19.5 | issue nula quando não houve escalação, lida como "o Execute não rodou ou teve sucesso" (*Propriedade 23*) | issue só em linha escalada: remediação falha ou `S_violation` escalada pelo `PlanAction` | controles detectivos, disjuntor e verificação escalam sem invocar o Execute |

## Glossário

- **Compliance_Loop**: O sistema completo de automação de conformidade baseado no padrão MAPE-K
- **Monitor**: Componente que detecta eventos de mutação em recursos cloud via CloudTrail e EventBridge
- **Analyze**: Componente Lambda que classifica o estado de conformidade do recurso usando OPA/Rego
- **Execute**: Componente Lambda que executa remediação automática usando OpenTofu
- **Knowledge_Base**: Banco de dados DynamoDB que armazena políticas, exceções aprovadas e log de violações *(revisado: a tabela `controls` guarda os metadados de cada controle e a chave da política; o texto Rego fica no S3 — ver 4.2)*
- **Orchestrator**: Step Functions Standard que coordena as transições entre componentes do laço
- **S_nominal**: Estado formal indicando que o recurso está conforme com a política vigente
- **S_exception**: Estado formal indicando que o recurso possui exceção aprovada e vigente
- **S_violation**: Estado formal indicando que o recurso está em violação sem exceção válida
- **Controle**: Conjunto de política Rego, evento CloudTrail e recurso alvo que define uma regra de conformidade *(revisado: também a remediação em OpenTofu, a chamada de leitura da configuração — `fetch_config` — e o modo de remediação, automático ou manual)*
- **Exceção**: Autorização temporária que permite desvio de conformidade para recurso específico
- **violations_log**: Registro persistente de todas as avaliações de conformidade no Knowledge_Base
- **Escalation_Queue**: Fila SQS FIFO que armazena violações não remediadas para notificação ao operador *(revisado: também falhas do sistema — ver 8.1)*
- **Remediação**: Ação automatizada que corrige configuração de recurso para estado conforme

Termos acrescentados com os requisitos 20–27:

- **Plan**: A etapa do MAPE-K que decide, para um recurso em S_violation, entre remediar e escalar. Não era um componente na especificação inicial (a decisão era implícita no Orchestrator); no sistema construído é o estado `PlanAction`
- **Rodada_de_Detecção**: A execução disparada por um evento de mutação; pode remediar
- **Rodada_de_Verificação**: A execução agendada por uma Rodada_de_Detecção que remediou; relê os controles remediados e nunca remedia
- **Disjuntor**: A regra do Plan que escala, sem remediar, um par (recurso, controle) já remediado repetidas vezes numa janela curta
- **Controle_Detectivo**: Controle cuja correção é disruptiva demais para ser automática (remediação `manual`): o sistema classifica e escala, sem tocar no recurso
- **Sessão_de_Remediação**: Credencial temporária da role de remediação, emitida por violação e restrita, por session policy, ao recurso classificado e às ações do controle
- **Agendamento_de_Exceção**: Agendamento do EventBridge Scheduler que reavalia o recurso quando a exceção expira

## Requisitos

### Requisito 1: Detectar Eventos de Mutação em Recursos Monitorados

**História de usuário:** Como operador de segurança, quero que o sistema detecte automaticamente mudanças em recursos cloud, para que nenhuma alteração de configuração passe despercebida.

#### Critérios de aceitação

1. QUANDO ocorrer um evento `PutBucketPublicAccessBlock` no CloudTrail, o Monitor DEVE capturar o evento em até 5 segundos *(não atendido — ver Revisões)*
2. O Monitor DEVE extrair o ARN do bucket do detalhe do evento
3. O Monitor DEVE extrair o ID da conta AWS do detalhe do evento
4. O Monitor DEVE extrair a região AWS do detalhe do evento
5. O Monitor DEVE acionar o Orchestrator com os metadados extraídos do evento
6. O Monitor DEVE processar apenas eventos correspondentes ao control_id "ctrl-s3-public-block" *(revisado)*

### Requisito 2: Orquestrar as Transições de Estado do Laço MAPE-K

**História de usuário:** Como arquiteto de sistema, quero que o laço MAPE-K seja orquestrado de forma determinística, para que cada evento siga o fluxo correto de análise e execução.

#### Critérios de aceitação

1. QUANDO o Monitor acionar o Orchestrator, o Orchestrator DEVE invocar o Analyze com os metadados do evento
2. QUANDO o Analyze retornar S_nominal, o Orchestrator DEVE encerrar o fluxo sem invocar o Execute
3. QUANDO o Analyze retornar S_exception, o Orchestrator DEVE encerrar o fluxo sem invocar o Execute
4. QUANDO o Analyze retornar S_violation, o Orchestrator DEVE invocar o Execute
5. QUANDO o Execute tiver sucesso, o Orchestrator DEVE encerrar o fluxo *(revisado)*
6. SE o Execute falhar, ENTÃO o Orchestrator DEVE acionar o mecanismo de escalação
7. O Orchestrator DEVE concluir o laço inteiro em até 60 segundos

### Requisito 3: Classificar o Estado de Conformidade do Recurso

**História de usuário:** Como analista de conformidade, quero que o sistema classifique com precisão o estado de cada recurso, para que as decisões de remediação se baseiem numa avaliação correta.

#### Critérios de aceitação

1. QUANDO o Analyze receber os metadados do evento, o Analyze DEVE obter da AWS a configuração atual do recurso
2. O Analyze DEVE avaliar a configuração do recurso contra a política Rego `s3_public_access` usando o OPA *(revisado)*
3. QUANDO o recurso estiver conforme a política, o Analyze DEVE retornar S_nominal
4. QUANDO o recurso não estiver conforme e tiver uma exceção válida, o Analyze DEVE retornar S_exception
5. QUANDO o recurso não estiver conforme e não tiver exceção válida, o Analyze DEVE retornar S_violation
6. O Analyze DEVE consultar a tabela de exceções do Knowledge_Base pelo índice de control_id, filtrando por resource_arn e por valid_until maior que o instante atual *(revisado)*
7. QUANDO existir uma exceção com valid_until no futuro, o Analyze DEVE considerá-la válida
8. QUANDO existir uma exceção com valid_until no passado, o Analyze DEVE considerá-la expirada
9. O Analyze DEVE produzir classificação determinística para entradas idênticas

### Requisito 4: Versionar as Políticas Rego em Repositório Git

**História de usuário:** Como administrador de políticas, quero que as políticas Rego sejam versionadas em Git, para que as mudanças sejam auditáveis pelo histórico de commits e revisadas por pull requests.

#### Critérios de aceitação

1. O Compliance_Loop DEVE versionar as políticas Rego num repositório Git
2. A Lambda Analyze DEVE carregar as políticas Rego do pacote de implantação em tempo de execução *(revisado)*
3. O arquivo da política Rego DEVE estar incluído no artefato de implantação da Lambda *(revisado)*
4. QUANDO uma política mudar, a mudança DEVE exigir nova implantação da Lambda Analyze *(revisado)*
5. A tabela `controls` do Knowledge_Base DEVE referenciar o nome do pacote da política, e NÃO DEVE armazenar o texto da política
6. O repositório Git DEVE ser a fonte única da verdade das definições de política

### Requisito 5: Gerenciar Exceções Aprovadas com TTL Nativo

**História de usuário:** Como gestor de conformidade, quero registrar exceções temporárias para recursos específicos, para que desvios autorizados não disparem remediação automática.

#### Critérios de aceitação

1. O Knowledge_Base DEVE armazenar as exceções na tabela de exceções
2. O Knowledge_Base DEVE usar exception_id (UUID) como chave de partição *(revisado — limitação L20)*
3. O Knowledge_Base DEVE indexar as exceções por control_id num índice secundário global *(revisado)*
4. O Knowledge_Base DEVE armazenar o resource_arn de cada exceção
5. O Knowledge_Base DEVE armazenar o control_id de cada exceção
6. O Knowledge_Base DEVE armazenar valid_until como timestamp Unix
7. O campo valid_until DEVE ser configurado como atributo de TTL no DynamoDB
8. O Analyze DEVE filtrar as exceções por valid_until > agora nas consultas
9. O mecanismo de TTL DEVE apagar os itens expirados eventualmente (em até 48 h), mas o comportamento do sistema NÃO DEVE depender da deleção física
10. O Knowledge_Base DEVE armazenar o texto de justificativa de cada exceção
11. O Knowledge_Base DEVE armazenar a identidade de quem aprovou cada exceção
12. O Knowledge_Base DEVE armazenar o instante de criação de cada exceção

### Requisito 6: Registrar o Log de Violações

**História de usuário:** Como auditor, quero que todas as avaliações de conformidade sejam registradas permanentemente, para que eu possa rastrear o histórico completo de violações e remediações.

#### Critérios de aceitação

1. O Analyze DEVE criar exatamente uma entrada na tabela violations_log para cada avaliação *(revisado)*
2. A entrada do violations_log DEVE usar violation_id (UUID) como chave de partição
3. A entrada do violations_log DEVE incluir resource_arn como atributo indexado
4. A entrada do violations_log DEVE incluir o control_id
5. A entrada do violations_log DEVE incluir o estado da classificação (S_nominal, S_exception ou S_violation)
6. A entrada do violations_log DEVE incluir o instante da avaliação em formato ISO 8601
7. A entrada do violations_log DEVE incluir a policy_version usada na avaliação
8. QUANDO a classificação for S_exception, a entrada do violations_log DEVE incluir a referência ao exception_id
9. QUANDO o Execute for invocado, a entrada do violations_log DEVE incluir o campo remediation_status
10. O campo remediation_status DEVE conter o valor "success" quando o Execute tiver sucesso
11. O campo remediation_status DEVE conter o valor "failed" quando o Execute falhar

### Requisito 7: Remediar Recursos em Violação

**História de usuário:** Como engenheiro de segurança, quero que recursos em violação sejam corrigidos automaticamente, para que a superfície de ataque seja reduzida sem intervenção manual.

#### Critérios de aceitação

1. QUANDO o Execute receber S_violation do Orchestrator, o Execute DEVE aplicar a configuração OpenTofu ao recurso
2. O Execute DEVE usar o resource_id recebido do Orchestrator para identificar o recurso-alvo
3. O Execute DEVE aplicar a configuração OpenTofu com a opção `-refresh=false`
4. A configuração OpenTofu DEVE descrever o estado conforme do recurso
5. QUANDO o `tofu apply` tiver sucesso, o Execute DEVE retornar status de sucesso ao Orchestrator
6. SE o `tofu apply` falhar, ENTÃO o Execute DEVE retornar status de falha com os detalhes do erro
7. O Execute DEVE operar de forma idempotente sobre o mesmo recurso
8. O Execute DEVE concluir a remediação em até 45 segundos
9. O Execute DEVE impor BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy e RestrictPublicBuckets em buckets S3

### Requisito 8: Escalar Violações Não Remediadas

**História de usuário:** Como operador de segurança, quero ser notificado quando a remediação automática falhar, para que eu possa intervir manualmente em casos complexos.

#### Critérios de aceitação

1. QUANDO o Execute retornar status de falha, o Orchestrator DEVE enviar uma mensagem à Escalation_Queue *(revisado: ampliado)*
2. A Escalation_Queue DEVE ser uma fila SQS FIFO
3. A mensagem de escalação DEVE incluir o resource_arn
4. A mensagem de escalação DEVE incluir o control_id
5. A mensagem de escalação DEVE incluir os detalhes do erro vindos do Execute
6. A mensagem de escalação DEVE incluir o instante da violação *(revisado)*
7. A mensagem de escalação DEVE incluir a referência ao violation_id
8. A mensagem de escalação DEVE incluir a step_functions_execution_url, para navegar até os detalhes da execução *(revisado)*
9. QUANDO uma mensagem entrar na Escalation_Queue, o Compliance_Loop DEVE criar uma issue no GitHub
10. A issue do GitHub DEVE incluir todos os detalhes da mensagem de escalação
11. A issue do GitHub DEVE receber os rótulos "compliance-violation" e "auto-escalation"
12. O github_issue_id devolvido pela API do GitHub DEVE ser gravado na entrada do violations_log

### Requisito 9: Garantir Idempotência na Remediação

**História de usuário:** Como desenvolvedor do sistema, quero que a remediação seja idempotente, para que execuções repetidas não causem efeitos colaterais indesejados.

#### Critérios de aceitação

1. QUANDO o Execute for invocado várias vezes para o mesmo recurso em S_violation, o Execute DEVE produzir o mesmo estado final do recurso
2. O Execute DEVE usar configuração declarativa do OpenTofu para garantir a idempotência
3. QUANDO o recurso já estiver em estado conforme, o `tofu apply` DEVE ter sucesso sem causar desvio de configuração
4. O Execute DEVE usar um backend de estado local e efêmero (`/tmp/remediation.tfstate`) *(revisado)*

### Requisito 10: Implementar o Controle S3 Public Block

**História de usuário:** Como especialista em segurança de S3, quero que o MVP implemente o controle de bloqueio de acesso público, para que buckets não sejam expostos inadvertidamente.

#### Critérios de aceitação

1. O Compliance_Loop DEVE implementar o control_id "ctrl-s3-public-block" *(revisado: ampliado)*
2. A política `s3_public_access` DEVE avaliar a configuração BlockPublicAcls
3. A política `s3_public_access` DEVE avaliar a configuração IgnorePublicAcls
4. A política `s3_public_access` DEVE avaliar a configuração BlockPublicPolicy
5. A política `s3_public_access` DEVE avaliar a configuração RestrictPublicBuckets
6. QUANDO qualquer uma das quatro configurações for falsa, a política `s3_public_access` DEVE retornar violação
7. QUANDO as quatro configurações forem verdadeiras, a política `s3_public_access` DEVE retornar conformidade

### Requisito 11: Validar o Cenário de Remediação Automática

**História de usuário:** Como testador do sistema, quero validar que a remediação automática funciona de ponta a ponta, para que eu possa confiar no comportamento do MVP.

#### Critérios de aceitação

1. QUANDO um bucket for criado com acesso público habilitado, o Compliance_Loop DEVE detectar a violação em até 10 segundos *(parcialmente atendido — ver Revisões)*
2. O Analyze DEVE classificar o recurso como S_violation
3. O Execute DEVE remediar o bucket para o estado conforme
4. O violations_log DEVE conter uma entrada com remediation_status "success"
5. QUANDO a configuração do bucket for consultada após a remediação, o bucket DEVE ter os quatro bloqueios de acesso público ativos

### Requisito 12: Validar o Cenário de Exceção Válida

**História de usuário:** Como testador do sistema, quero validar que exceções aprovadas impedem a remediação, para que recursos autorizados não sejam modificados indevidamente.

#### Critérios de aceitação

1. QUANDO um bucket tiver acesso público habilitado e existir uma exceção válida, o Analyze DEVE classificar o recurso como S_exception
2. O Orchestrator DEVE encerrar sem invocar o Execute
3. O violations_log DEVE conter uma entrada com a classificação S_exception
4. A entrada do violations_log DEVE referenciar o exception_id
5. A configuração do bucket DEVE permanecer inalterada

### Requisito 13: Validar o Cenário de Exceção Expirada

**História de usuário:** Como testador do sistema, quero validar que exceções expiradas não impedem a remediação, para que proteções temporárias não se tornem permanentes.

#### Critérios de aceitação

1. QUANDO um bucket tiver acesso público habilitado e existir uma exceção expirada, o Analyze DEVE classificar o recurso como S_violation
2. O Analyze DEVE ignorar a exceção expirada durante a classificação
3. O Execute DEVE remediar o bucket para o estado conforme
4. O violations_log DEVE conter uma entrada com a classificação S_violation e remediation_status "success"

### Requisito 14: Validar o Cenário de Falha na Remediação

**História de usuário:** Como testador do sistema, quero validar que falhas de remediação são escaladas corretamente, para que os operadores sejam notificados dos problemas.

#### Critérios de aceitação

1. QUANDO o Execute não conseguir remediar um recurso, o Orchestrator DEVE enviar uma mensagem à Escalation_Queue
2. A Escalation_Queue DEVE conter uma mensagem com os detalhes do erro
3. O Compliance_Loop DEVE criar uma issue no GitHub com os detalhes da violação
4. O violations_log DEVE conter uma entrada com o estado S_violation e remediation_status "failed"
5. A configuração do bucket DEVE permanecer em estado não conforme

### Requisito 15: Implementar o Parser de Eventos do CloudTrail

**História de usuário:** Como desenvolvedor, quero interpretar os eventos do CloudTrail de forma confiável, para que os metadados dos recursos sejam extraídos corretamente.

#### Critérios de aceitação

1. QUANDO o Monitor receber um evento do CloudTrail, o Monitor DEVE interpretar a estrutura JSON do evento
2. O Monitor DEVE extrair o campo `detail.eventName`
3. O Monitor DEVE extrair o campo `detail.requestParameters.bucketName` *(revisado: ampliado)*
4. O Monitor DEVE extrair o campo `detail.userIdentity.accountId`
5. O Monitor DEVE extrair o campo `detail.awsRegion`
6. SE algum campo obrigatório estiver ausente, ENTÃO o Monitor DEVE registrar o erro e descartar o evento *(revisado)*
7. O Monitor DEVE construir o ARN do recurso no formato "arn:aws:s3:::nome-do-bucket" *(revisado: ampliado)*

### Requisito 16: Garantir Determinismo na Avaliação de Políticas

**História de usuário:** Como desenvolvedor de políticas, quero que a avaliação das políticas seja determinística, para que o mesmo recurso produza sempre a mesma classificação.

#### Critérios de aceitação

1. QUANDO o Analyze avaliar a mesma configuração de recurso várias vezes, o Analyze DEVE retornar a mesma classificação
2. O Analyze NÃO DEVE depender de estado externo além do Knowledge_Base *(revisado)*
3. O Analyze DEVE usar um instante consistente nas verificações de expiração de exceções dentro de uma mesma avaliação
4. A avaliação do OPA NÃO DEVE usar funções embutidas não determinísticas

### Requisito 17: Implementar Timeout nos Componentes

**História de usuário:** Como engenheiro de confiabilidade, quero que os componentes tenham timeout configurado, para que operações lentas não bloqueiem o laço indefinidamente.

#### Critérios de aceitação

1. O Analyze DEVE expirar após 15 segundos *(revisado)*
2. O Execute DEVE expirar após 45 segundos *(revisado)*
3. O Orchestrator DEVE expirar o fluxo inteiro após 60 segundos *(revisado)*
4. QUANDO um componente expirar, o Orchestrator DEVE registrar o erro de timeout e encerrar o fluxo *(revisado)*

### Requisito 18: Persistir o Estado S_violation Após Falha de Remediação

**História de usuário:** Como auditor, quero que o estado de violação persista quando a remediação falha, para que o registro reflita a realidade do recurso, e não a do processo.

#### Critérios de aceitação

1. QUANDO o Execute não conseguir remediar um recurso, a entrada do violations_log DEVE manter o estado S_violation
2. A entrada do violations_log NÃO DEVE criar um novo estado S_remediation_failed
3. O campo remediation_status DEVE indicar a falha, e não o campo de estado
4. O campo de estado DEVE representar o estado real de conformidade do recurso
5. QUANDO o Execute falhar, o violations_log DEVE conter exatamente uma entrada com o estado S_violation e remediation_status "failed"

### Requisito 19: Rastrear a Escalação pelo ID da Issue no GitHub

**História de usuário:** Como operador, quero que as violações escaladas sejam rastreáveis até a issue de resolução, para que eu possa auditar o ciclo completo, da detecção ao encerramento.

#### Critérios de aceitação

1. QUANDO uma issue do GitHub for criada para uma remediação falha, o Compliance_Loop DEVE capturar o número da issue devolvido pela API do GitHub *(revisado: ampliado)*
2. O github_issue_id DEVE ser gravado na entrada do violations_log correspondente à violação
3. A entrada do violations_log DEVE ligar o evento de violação ao fluxo operacional de resolução
4. QUANDO o violations_log for consultado por violation_id, o resultado DEVE incluir o github_issue_id se houve escalação
5. O campo github_issue_id DEVE ser nulo para violações que não exigiram escalação *(revisado)*

## Requisitos acrescentados durante o desenvolvimento

Os requisitos abaixo não estavam na especificação inicial. Cada um nasceu de um problema encontrado ao construir ou medir o sistema, e está registrado com a mesma forma dos anteriores (história de usuário e critérios EARS) para que a matriz de rastreabilidade os cubra do mesmo modo. A motivação de cada um está na seção do `design.md` indicada.

### Requisito 20: Verificar a Convergência da Remediação

**História de usuário:** Como auditor, quero que cada remediação seja confirmada por uma nova leitura do recurso, para que o registro diga que o recurso convergiu, e não apenas que o comando de remediação terminou sem erro.

*Motivação: a primeira versão confirmava a convergência pelo eco da própria remediação no CloudTrail, um evento que o sistema provoca (design.md, Fechamento do laço em duas rodadas).*

#### Critérios de aceitação

1. QUANDO uma Rodada_de_Detecção remediar com sucesso ao menos um controle, o Orchestrator DEVE agendar exatamente uma Rodada_de_Verificação para aquele recurso
2. A Rodada_de_Verificação DEVE reavaliar apenas os controles remediados, lendo a configuração do recurso sem espera fixa
3. A Rodada_de_Verificação NÃO DEVE invocar o Execute e NÃO DEVE agendar outra Rodada_de_Verificação
4. QUANDO a Rodada_de_Verificação classificar um controle remediado como S_violation, o Orchestrator DEVE escalá-lo com o motivo "non_convergence"
5. SE a Rodada_de_Verificação não puder ser agendada ou não puder avaliar os controles remediados, ENTÃO o Orchestrator DEVE escalar o incidente
6. A entrada do violations_log de uma Rodada_de_Verificação DEVE referenciar a execução de origem e o violation_id que ela verifica
7. O Orchestrator DEVE executar no máximo duas vezes por incidente, qualquer que seja o número de controles remediados

### Requisito 21: Interromper Remediações Reincidentes

**História de usuário:** Como engenheiro de confiabilidade, quero que o sistema pare de remediar um recurso que algo continua revertendo, para que dois automatismos não disputem o mesmo recurso indefinidamente.

*Motivação: sem limite, uma remediação revertida por outro processo (ou recusada pela própria política) se repete a cada evento (design.md, L11).*

#### Critérios de aceitação

1. QUANDO uma Rodada_de_Detecção classificar como S_violation um controle de remediação automática, o Plan DEVE contar, antes de invocar o Execute, as remediações bem-sucedidas do mesmo par (recurso, controle) numa janela configurável (padrão de 10 minutos)
2. QUANDO a contagem atingir um limiar configurável (padrão 3), o Plan DEVE escalar com o motivo "recurrent_violation" em vez de invocar o Execute
3. SE a contagem não puder ser lida, ENTÃO o Plan DEVE prosseguir como se não houvesse remediação recente, e a decisão DEVE ficar registrada no histórico da execução

### Requisito 22: Suportar Controles Detectivos

**História de usuário:** Como engenheiro de segurança, quero registrar controles que apenas detectam e escalam, para que violações cuja correção é disruptiva (como criptografar um volume em uso) sejam tratadas por uma pessoa.

#### Critérios de aceitação

1. A tabela `controls` DEVE permitir que um controle declare sua remediação como "automatic" ou "manual"
2. QUANDO um controle de remediação manual for classificado como S_violation, o Plan DEVE escalar com o motivo "manual_remediation_required" e NÃO DEVE invocar o Execute
3. O recurso sob um Controle_Detectivo DEVE permanecer inalterado pelo sistema

### Requisito 23: Limitar a Autoridade do Sistema ao Mínimo Necessário

**História de usuário:** Como responsável pela segurança da conta, quero que o sistema de conformidade só consiga ler e alterar o que cada incidente exige, para que ele próprio não se torne o maior risco da conta.

*Motivação: um controle é um dado numa tabela; sem limite, um controle adulterado dirigiria a remediação para qualquer recurso (design.md, Autoridade por incidente; Modelo de Permissões).*

#### Critérios de aceitação

1. O Execute DEVE remediar usando uma Sessão_de_Remediação emitida por violação, cujo nome de sessão é o violation_id
2. A Sessão_de_Remediação DEVE ser restrita ao ARN do recurso classificado e às ações do controle que está sendo remediado
3. SE um controle pedir uma ação fora da lista de ações de remediação permitidas, ENTÃO o Execute DEVE recusá-la sem executar o OpenTofu
4. A lista de ações de remediação permitidas DEVE ter uma definição única, compartilhada pela role de remediação e pela checagem do Execute
5. A role do Analyze DEVE conceder exatamente as ações de leitura que os controles cadastrados usam, e nenhuma outra
6. QUANDO um template de remediação visar um recurso diferente do classificado, a escrita DEVE ser negada pelo IAM

### Requisito 24: Reavaliar o Recurso na Expiração da Exceção

**História de usuário:** Como gestor de conformidade, quero que o recurso seja reavaliado quando a exceção expira, para que a proteção volte sem depender de um novo evento no recurso.

*Motivação: o critério 3.8 só garante que uma exceção expirada é ignorada na próxima avaliação, que pode nunca acontecer; o TTL do DynamoDB leva até 48 h e não dispara avaliação (design.md, reavaliação por Scheduler).*

#### Critérios de aceitação

1. QUANDO uma exceção for criada ou modificada, o Compliance_Loop DEVE criar um Agendamento_de_Exceção para valid_until mais 30 segundos
2. QUANDO o Agendamento_de_Exceção disparar, o Orchestrator DEVE avaliar o recurso para o controle da exceção
3. QUANDO uma exceção for apagada, o Compliance_Loop DEVE apagar o seu Agendamento_de_Exceção
4. O Agendamento_de_Exceção NÃO DEVE depender da deleção física da exceção pelo TTL

### Requisito 25: Atualizar os Controles sem Nova Implantação

**História de usuário:** Como administrador de políticas, quero publicar ou remover um controle sem reimplantar funções, para que a cobertura de conformidade evolua na velocidade das políticas.

#### Critérios de aceitação

1. QUANDO um controle for inserido, modificado ou removido na tabela `controls`, o Compliance_Loop DEVE regenerar a regra do EventBridge a partir da união dos eventos dos controles cadastrados
2. O Analyze DEVE ler a configuração do recurso pela chamada declarada no controle (`fetch_config`), restrita a operações de leitura
3. QUANDO um controle novo usar apenas ações de leitura e de remediação já concedidas, o Compliance_Loop DEVE passar a avaliá-lo sem nova implantação de código das Lambdas

### Requisito 26: Aceitar Apenas Eventos Legítimos

**História de usuário:** Como responsável pela segurança da conta, quero que o Monitor só aceite eventos emitidos pela AWS, para que ninguém dispare o laço com um evento forjado nem o sistema reaja às próprias remediações.

#### Critérios de aceitação

1. O Monitor DEVE aceitar apenas eventos cuja origem seja emitida pela AWS (prefixo "aws.")
2. O Monitor NÃO DEVE aceitar eventos cuja identidade seja a role de remediação, para que uma remediação não dispare uma nova Rodada_de_Detecção

### Requisito 27: Tornar Visíveis as Falhas Silenciosas

**História de usuário:** Como operador, quero ser avisado das falhas que não geram escalação, para que o laço parado não pareça o laço funcionando.

*Motivação: uma detecção que o EventBridge não entrega, um agendamento que falha ou uma Lambda que cai não produzem linha nem issue (design.md, Operação: alarmes e runbook).*

#### Critérios de aceitação

1. O Compliance_Loop DEVE emitir um alarme para cada modo de falha que não produz escalação: detecção não entregue, falha no agendamento de exceção, queda ou timeout de Lambda, timeout de execução, limitação de taxa (*throttling*) e mensagens em filas de mensagens mortas
2. QUANDO um alarme entrar em ALARM, o Compliance_Loop DEVE abrir uma issue no GitHub com o rótulo "operations-alarm", ou comentar na issue aberta do mesmo alarme
3. QUANDO um alarme voltar a OK, o Compliance_Loop DEVE comentar na issue aberta, sem fechá-la
4. SE o Monitor não conseguir iniciar o Orchestrator, ENTÃO o Monitor DEVE guardar a detecção numa fila de mensagens mortas, da qual ela possa ser reprocessada
5. O Knowledge_Base DEVE manter recuperação pontual (PITR) nas tabelas violations_log e de exceções, e o Compliance_Loop DEVE definir um período de retenção para os logs das funções
