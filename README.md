# Monitor de promoções do Telegram — MySQL e Docker

Uma aplicação Python. O Telethon conecta duas sessões:
- **usuário**: sua conta pessoal, que já está nos canais/grupos de ofertas;
- **bot**: bot criado pelo BotFather, que recebe seus comandos privados e envia cópias das mensagens correspondentes.

O MySQL mantém frases de interesse, canais escolhidos e IDs de mensagens já notificadas. As tabelas são criadas automaticamente, começando vazias. O SQLite antigo não é utilizado.

## Preparar a sessão uma vez

Copie `.env.example` para `.env` e preencha `TG_API_ID`, `TG_API_HASH` (de https://my.telegram.org), `TG_BOT_TOKEN` (do @BotFather) e as duas senhas MySQL. Instale as dependências com Python 3.10+:

```sh
pip install -r requirements.txt
```

Se já possui a sessão local, converta sem telefone nem código:

```sh
python generate_session.py --from-session data/usuario.session
```

Para uma conta ainda não autenticada, execute `python generate_session.py` e informe telefone, código e senha 2FA, se houver. Essa etapa ocorre uma única vez, fora do processo principal. A ferramenta grava `TG_USER_SESSION` em `data/telegram.env`; copie essa variável para `.env` ou para os secrets do seu gerenciador de containers. O arquivo de saída não é sobrescrito automaticamente.

O container recebe a sessão pronta e nunca pede entrada no terminal. Sessão revogada exige gerar outra. [Documentação de sessões do Telethon](https://docs.telethon.dev/en/stable/concepts/sessions.html).

## Subir com MySQL

```sh
docker compose up -d --build
```

O Compose inicia MySQL 8.4, aguarda sua disponibilidade e inicia o monitor. O volume `mysql_data` conserva os dados. Execute uma única instância do monitor por conta. Abra o bot com a mesma conta e envie `/start` para permitir o recebimento de mensagens.

Para cadastrar frases e canais automaticamente, configure no `.env` (IDs obtidos com `/grupos` no bot):

```dotenv
INITIAL_KEYWORDS=["rtx 5060","notebook gamer"]
INITIAL_CHAT_IDS=[-1001079131412]
```

São arrays JSON opcionais; use `[]` para começar vazio. A carga é aditiva e reaplicada a cada início. Para remover algo definitivamente pelo bot, remova também da configuração inicial.

## Levar a imagem para outro servidor

```sh
docker build -t telegram-ofertas-bot:latest .
docker save -o telegram-ofertas-bot.tar telegram-ofertas-bot:latest
```

Transfira `telegram-ofertas-bot.tar`, `compose.yaml` e `.env` separadamente. No destino:

```sh
docker load -i telegram-ofertas-bot.tar
docker compose up -d --no-build
```

A imagem é Linux/amd64 quando construída em um PC Intel/AMD. O destino deve suportar essa arquitetura. Credenciais e sessões não são incluídas na imagem.

Se já tem MySQL externo, crie o banco e o usuário com permissão para criar/alterar as tabelas de dados. Configure `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER` e `MYSQL_PASSWORD` no ambiente do container e inicie apenas a aplicação:

```sh
docker run -d --name telegram-ofertas-bot --restart unless-stopped --init --env-file .env telegram-ofertas-bot:latest
```

Para `docker run --env-file`, escreva os valores sem aspas externas, como no `.env.example`. Não há porta HTTP para publicar. Sem Docker, configure `MYSQL_HOST` com o endereço do servidor e execute `python app.py`.

## Compartilhar a instância

A conta de `TG_USER_SESSION` continua lendo os canais. Para permitir que outras
pessoas usem o mesmo bot, configure os IDs numéricos delas:

```dotenv
TG_ALLOWED_USER_IDS=[123456789,987654321]
```

No Secret Kubernetes, dentro de `stringData`, o valor precisa ser texto:

```yaml
  TG_ALLOWED_USER_IDS: "[123456789,987654321]"
```

Os números acima são exemplos. O dono da sessão permanece autorizado
automaticamente; `[]` mantém o uso exclusivo pelo dono. Cada pessoa adicionada
precisa abrir o bot e enviar `/start`. Todos os autorizados podem consultar e
alterar os mesmos filtros/canais e recebem os mesmos alertas. Os canais
continuam sendo os acessíveis pela conta de `TG_USER_SESSION`.

Atualize a imagem e reinicie o Deployment depois de aplicar a configuração.
Falha ao enviar para uma pessoa não impede o envio para as demais; entregas
individuais que falharem não são recuperadas automaticamente.

## Comandos

| Comando | Exemplo | Ação |
|---|---|---|
| `/start` ou `/ajuda` | `/start` | Instruções |
| `/grupos` | `/grupos` | Lista canais/grupos aos quais sua conta tem acesso |
| `/monitorar <ids…>` | `/monitorar -1001079131412 -1009876543210 @canal` | Começa a monitorar vários de uma vez |
| `/monitorar todos` | `/monitorar todos` | Monitora todos os grupos/canais da conta |
| `/parar <ids…>` | `/parar -1001079131412 -1009876543210` | Para de monitorar |
| `/add <frase> [\| valor]` | `/add rtx 5060 \| 2000` | Adiciona palavra-chave/frase, com preço máximo opcional em R$ (`até 2000` também vale) |
| `/preco <frase> \| <valor>` | `/preco rtx 5060 \| 1800` | Define ou troca o preço máximo; `/preco rtx 5060 sem` remove |
| `/remover <frase>` | `/remover rtx 5060` | Remove frase |
| `/lista` | `/lista` | Lista as palavras-chave com um botão por frase (💰 tem limite, ➕ sem limite): toque, envie o valor e pronto; `0` remove o limite |
| `/teste <texto>` | `/teste RTX 5060 R$ 2000` | Testa correspondência e mostra o preço identificado |
| `/similar <a> \|\| <b>` | `/similar RTX 5060 R$ 2299 \|\| Asus RTX 5060 R$ 2149` | Mostra se dois anúncios seriam agrupados |
| `/status` | `/status` | Exibe números de grupos/frases |

## Como é feita a comparação?

A frase é comparada ignorando maiúsculas/minúsculas, acentos e pontuação. Por exemplo, `/add rtx 5060` corresponde a `RTX-5060 por R$ 1999`, mas não a `RTX 5060Ti`.

## Preço máximo por palavra-chave

Cada frase pode ter um valor máximo em R$. O alerta só sai se o preço lido no
anúncio for **menor ou igual** ao limite; acima disso a mensagem é ignorada. O
preço é o mesmo usado na deduplicação (valor final, sem parcelas). Anúncio em
que não dá para identificar nenhum preço **passa**, porque não há como comparar
e perder uma oferta custa mais que receber uma a mais. Frases sem limite não
filtram nada. Bancos MySQL existentes ganham a coluna `max_price` sozinhos na
próxima inicialização.

## Anúncios repetidos em vários grupos

Vários canais divulgam a mesma promoção quase ao mesmo tempo, com textos
diferentes e link de afiliado próprio. Em vez de enviar na hora, a aplicação
segura as mensagens correspondentes por `DEDUP_WINDOW_SECONDS` (5 por padrão),
compara o lote e entrega **um alerta por anúncio**, escolhendo o de **menor
preço em R$**. Empate ou anúncio sem preço: vale o primeiro que chegou. Os
descartados ficam registrados como notificados e não voltam.

A comparação usa só o conteúdo do produto: links e valores são removidos antes,
porque mudam de canal para canal. Dois anúncios são considerados o mesmo quando:

- os links apontam para o **mesmo código de produto** (Amazon `/dp/`, Mercado
  Livre `MLB-…`, Shopee, AliExpress, KaBuM, Magalu) — códigos diferentes separam
  os anúncios mesmo com texto idêntico; ou
- o vocabulário do produto coincide acima de `DEDUP_SIMILARITY` (0.65 por
  padrão), **e** não há conflito de medidas (`1TB` × `2TB`, `50` × `65`
  polegadas) nem de modelos (`G502` × `G203`, `510BT` × `520BT`).

Na dúvida o alerta é enviado duas vezes, nunca escondido: perder uma oferta
diferente custa mais caro que receber uma repetição. Para calibrar o limite use
`/similar texto A || texto B`, que mostra a nota dos dois e o preço de cada um.

Com `SHOW_SOURCE=1` (padrão) cada alerta começa com `Fonte: nome do grupo`, e
quando houve descarte a linha seguinte diz em quais grupos o mesmo anúncio
apareceu. Use `SHOW_SOURCE=0` para voltar ao texto sem cabeçalho.

## O que o bot encaminha?

- Texto é enviado **sem alterações intencionais**, mantendo links, emojis e formatação quando possível; o único acréscimo é a linha `Fonte:` controlada por `SHOW_SOURCE`.
- Se a postagem tem foto/arquivo até `MAX_MEDIA_MB`, a aplicação tenta copiar a mídia e preservar a legenda original. Caso não consiga, registra erro no console; não é um encaminhamento nativo da mensagem original porque o bot não participa do canal.
- Se a mídia for maior que o limite, envia apenas o texto original.
- Mensagens com encaminhamento restrito são ignoradas.
- Apenas mensagens **novas após o programa conectar** serão detectadas. Não vasculha histórico.
- Sem IA e sem consulta de preços nesta primeira etapa.

## Segurança e limites

- Proteja `.env`, `data/telegram.env` e `data/*.session`: a sessão permite acesso à sua conta.
- O bot só atende mensagens privadas do dono e dos IDs configurados em `TG_ALLOWED_USER_IDS`.
- O bot só consegue enviar mensagens depois que você abre a conversa e envia `/start`.
- O MySQL mantém palavras-chave e grupos escolhidos após reiniciar a aplicação.
- Se o bot perder conexão, mensagens podem deixar de ser capturadas; não existe recuperação de eventos perdidos nesta versão.
- Para respeitar as restrições de canais, evite reproduzir conteúdo que não pode ser redistribuído e obtenha as autorizações necessárias.
- A aplicação não usa n8n, webhooks, scraping nem recebe senhas de terceiros.

## Testes locais

Execute `python -m unittest discover -s tests -v` para testar a comparação de palavras-chave, a leitura de preços e o agrupamento de anúncios repetidos.
