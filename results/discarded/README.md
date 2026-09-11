# Rodadas descartadas

Rodadas preservadas mas **excluídas da agregação**, com o motivo. Ficam aqui em
vez de serem apagadas para que a exclusão seja auditável — descartar dado é
legítimo desde que declarado, não silencioso.

## run-04-credential-rotation.json

`CTRL-02`, `CTRL-03` e `CTRL-04` falharam com:

```
InvalidAccessKeyId: The AWS Access Key Id you provided does not exist in our records.
```

A chave de acesso do runner foi rotacionada durante a execução (de
`shared-credentials-file` para o provider `login` do AWS CLI). As três falhas
ocorreram no `create_bucket`, **antes** de qualquer envolvimento do loop de
compliance — nenhuma delas testou o sistema.

Mantê-la na agregação reportaria `CTRL-02: 4/5`, sugerindo 80% de
confiabilidade do sistema quando o que falhou foi a credencial do runner. O
critério de exclusão: *uma rodada em que o harness não conseguiu falar com a
AWS não testou o sistema* — equivalente a queda de energia no meio do
experimento.

Os 9 cenários que passaram nesta rodada são consistentes com as demais e estão
preservados no arquivo para conferência.
