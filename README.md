# 네이버 폐지 줍기 (독립 실행 / cron용)

flaskfarm 플러그인 [`ff_naverpaper`](https://gitlab.com/sukajds/ff_naverpaper)를 플러그인 없이 단독으로 실행하도록 바꾼 버전입니다.
수집 엔진(`source_naverpaper.py`)은 원본 그대로이고, 실행부만 `naverpaper.py`(CLI)로 대체했습니다.

- cron으로 1회 실행하고 종료
- 실행마다 이번 적립 금액과 계정별 누적 합계 출력
- 디스코드 웹훅(스레드 지정 가능)으로 결과 전송
- 캡차로 자동 로그인이 막힐 때 터미널에서 수동 로그인
- MCP 서버(`naverpaper_mcp.py`): LiteLLM 등에서 `naverpaper_` 도구로 조회·실행

---

## 1. 설치 (예: `/mnt/naverpaper`)

### 1-1. 사전 준비

```bash
sudo apt install -y git python3 python3-venv python3-pip
```

### 1-2. 내려받기

```bash
sudo mkdir -p /mnt/naverpaper
sudo chown $USER:$USER /mnt/naverpaper      # root 로 작업 중이면 생략
git clone https://github.com/yume-script/naverpaper.git /mnt/naverpaper
cd /mnt/naverpaper
ls      # naverpaper.py, source_naverpaper.py, requirements.txt 가 보여야 함
```

git 없이 zip 으로 받을 때(`unzip` 필요):

```bash
cd /mnt/naverpaper
wget https://github.com/yume-script/naverpaper/archive/refs/heads/main.zip
unzip main.zip && mv naverpaper-main/* . && rm -r naverpaper-main main.zip
```

기본 브랜치 이름이 `main`이 아니면 주소의 `main` 부분을 바꾸세요.

### 1-3. 폴더가 이미 있을 때

`fatal: destination path '/mnt/naverpaper' already exists and is not an empty directory` 오류가 나면 먼저 안을 확인하세요. 이미 설정한 `config.json`이나 `data/`(쿠키, DB)가 있을 수 있습니다.

```bash
ls -al /mnt/naverpaper
```

| 상황 | 방법 |
| --- | --- |
| 안에 `.git`이 있음 (이미 클론됨) | `cd /mnt/naverpaper && git pull` |
| `.git`이 없고 파일만 있음 | 아래 "코드만 덮어쓰기" |
| 비어 있거나 필요 없는 폴더 | 이름을 바꿔 두고 새로 클론 |

코드만 덮어쓰기 (`config.json`, `data/`는 저장소에 없으므로 그대로 유지됨):

```bash
cp -a /mnt/naverpaper /mnt/naverpaper.bak                      # 불안하면 먼저 백업
git clone https://github.com/yume-script/naverpaper.git /tmp/naverpaper-src
cp -r /tmp/naverpaper-src/. /mnt/naverpaper/
rm -rf /tmp/naverpaper-src
```

필요 없는 폴더를 치우고 새로 받기:

```bash
mv /mnt/naverpaper /mnt/naverpaper.old
git clone https://github.com/yume-script/naverpaper.git /mnt/naverpaper
```

저장소 안에 `naverpaper/` 폴더가 한 겹 더 있다면, 그 폴더의 내용을 `/mnt/naverpaper/`로 복사해서 쓰세요.

### 1-4. 파이썬 환경과 브라우저

```bash
cd /mnt/naverpaper
python3 -m venv venv              # 이미 venv/ 가 있으면 건너뜀
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium  # 브라우저 구동용 시스템 라이브러리 (root 또는 sudo)
```

`install-deps`는 서버에 GUI 라이브러리가 없으면 chromium이 실행되지 않기 때문에 필요합니다.

### 1-5. 설정 파일

```bash
cp -n config.example.json config.json    # config.json 이 이미 있으면 유지
chmod 600 config.json                    # 비밀번호가 들어 있으니 권한 제한
nano config.json
```

### 1-6. 첫 실행

```bash
python3 naverpaper.py status             # 계정별 쿠키 파일 상태 (처음엔 NG 가 정상)
python3 naverpaper.py                    # 1회 수집
```

처음 실행하면 비밀번호로 자동 로그인을 시도합니다. 캡차로 막히면 `python3 naverpaper.py login 아이디` 를 사용하세요(아래 "수동 로그인").

### 1-7. cron 등록

```bash
crontab -e
```

```cron
0 3,9,15,21 * * * cd /mnt/naverpaper && ./venv/bin/python naverpaper.py
```

- 로그는 `data/naverpaper.log` 에 자동 저장됩니다 (2MB x 4개 순환).
- 이전 실행이 끝나기 전에 다시 cron이 돌면 파일 락으로 건너뜁니다 (종료 코드 2).
- `/mnt`가 외장 디스크나 네트워크 마운트라면 부팅 직후 마운트 전에 cron이 돌 수 있습니다. 실행이 안 될 때는 `ls /mnt/naverpaper` 로 마운트 상태를 확인하세요.

### 1-8. 업데이트

```bash
cd /mnt/naverpaper
git pull
source venv/bin/activate
pip install -r requirements.txt     # 의존성이 바뀌었을 때만
```

`config.json`과 `data/`는 저장소에 올라가지 않으므로 그대로 유지됩니다.

---

## 2. 설정 (config.json)

| 키 | 설명 |
| --- | --- |
| accounts[].user_id / password | 네이버 계정 (여러 개 가능) |
| accounts[].keep_login | 로그인 상태 유지 체크 (기본 true) |
| accounts[].nid_aut / nid_ses / nid_jst | 선택. 브라우저에서 복사한 쿠키를 직접 넣을 때 (저장된 쿠키가 없거나 만료됐을 때만 사용) |
| no_paper_record | 방문할 URL이 없을 때도 결과 DB에 기록 (기본 true) |
| keep_campaign_days / keep_user_days | 오래된 캠페인 URL / 사용자 세션 기록 보관 일수 (기본 60 / 7) |
| proxy_url | 프록시 주소 |
| proxy_enabled / proxy_reward_enabled / proxy_login_enabled | 프록시 사용 범위 (전체 / 적립 / 로그인) |
| discord_webhook_url | 디스코드 웹훅 주소 (아래 참고) |
| discord_notify_mode | `always` 매번 전송 / `earned` 적립이 있거나 오류일 때만 전송 |
| discord_mask_ids | true 면 아이디를 앞 3글자만 표시 (`abc•••`) |
| stall_alert_minutes | 수집 중 새 로그가 이 시간(분) 동안 없으면 "진행 멈춤" 디스코드 알림. 기본 15, 0이면 끔 |
| max_run_minutes | 한 번의 실행이 이 시간(분)을 넘기면 "시간 초과" 알림. 기본 60, 0이면 끔 |
| mcp_token | MCP 서버(`naverpaper_mcp.py`) 인증 토큰. 외부 주소에 바인딩하면 필수 (7장 참고) |
| data_dir | 쿠키·DB·로그 저장 위치 (비우면 `./data`) |

---

## 3. 사용법

```bash
python3 naverpaper.py                # 1회 수집 후 종료 (= run)
python3 naverpaper.py total          # 적립 합계 + 최근 실행 내역 (-n 30 으로 개수 지정)
python3 naverpaper.py notify         # 적립 합계를 디스코드로 전송 (수집 없음)
python3 naverpaper.py notify --test  # 디스코드 테스트 메시지
python3 naverpaper.py set-points 번호 아이디 적립액 [--yes]  # 잘못 기록된 실행의 적립액 수정 (번호는 total 에서 확인)
python3 naverpaper.py status         # 계정별 쿠키 파일 상태 (만료일 기준)
python3 naverpaper.py verify [아이디] # 네이버에 실제 접속해 로그인 상태 확인 (계정당 10~20초)
python3 naverpaper.py login 아이디    # 수동 로그인 (캡차 대응)
python3 naverpaper.py link URL       # 링크 하나만 수동 적립
python3 naverpaper.py -c 다른설정.json run   # 설정 파일 경로 지정
python3 naverpaper.py -v             # 자세한 로그
```

종료 코드: `0` 정상 / `1` 수집 오류 / `2` 이미 실행 중이라 건너뜀 / `3` 설정·패키지 오류

### 세션 만료 감지와 자동 재로그인

쿠키 파일의 만료일이 남아 있어도 네이버가 서버에서 세션을 무효화하면, 네이버페이 페이지가 로그인 화면(`nid.naver.com/nidlogin...`)으로 이동합니다. 원본은 이를 감지하지 못해 **적립 없이 링크의 "방문 기록"만 남기고**, 로그인을 고쳐도 그 링크를 다시 시도하지 않았습니다. 이 버전은 다음과 같이 처리합니다.

1. 수집 단계(미션 페이지, 포인트 메인)와 계정 처리 단계(포인트 내역 페이지), 링크 처리 중에 로그인 화면으로 이동하면 세션 만료로 판단합니다.
2. 만료로 판단되면 **링크를 처리하지 않고 방문 기록도 남기지 않습니다.** 나중에 다시 시도됩니다.
3. 저장된 쿠키와 브라우저 프로필 쿠키를 버리고 **config.json의 비밀번호로 재로그인**한 뒤, 같은 실행 안에서 한 번 더 처리합니다. 수집 단계에서 만료가 확인되면 네이버페이 미션 소스도 다시 수집합니다.
4. 재로그인은 계정당 한 번의 실행에서 한 번만 시도하고, **30분 안에 다시 시도하지 않습니다**(`data/cookies/<아이디>.relogin` 파일 시각 기준). 캡차 등으로 반복 로그인해 계정이 잠기는 것을 막기 위해서입니다.
5. 재로그인이 실패하면 무효 쿠키 파일을 지워 `status`에 NG로 보이게 하고, 디스코드로 경고를 보냅니다. 이때는 아래 수동 로그인을 하세요.

디스코드 알림은 다음 두 가지가 추가됩니다. (`discord_notify_mode`가 `earned`여도 항상 전송됩니다.)

- 빨간색 "로그인 확인 필요": 세션 만료나 로그인 실패로 처리하지 못한 계정이 있을 때
- 결과 메시지의 `[알림]` 줄: 만료됐지만 자동 재로그인에 성공해 정상 처리된 경우

**세션 쿠키 허용**: 네이버가 "로그인 상태 유지" 없이 로그인시키면 쿠키에 만료일이 없는 세션 쿠키가 됩니다. 원본은 이를 항상 무효로 취급해 매 실행마다 비밀번호로 다시 로그인했습니다(캡차 위험, 시간 증가). 이 버전은 세션 쿠키도 저장된 상태로 재사용하고, 실제 유효 여부는 위의 로그인 화면 이동 감지로 판단합니다.

현재 상태를 직접 확인하려면 `verify`를 쓰세요. `status`는 쿠키 파일의 만료일만 보지만 `verify`는 네이버페이에 실제로 접속합니다.

```bash
./venv/bin/python naverpaper.py verify            # 전체 계정
./venv/bin/python naverpaper.py verify 아이디      # 한 계정
```

`OK` 정상 / `NG` 세션 만료(수동 로그인 필요) / `??` 판단 불가(페이지를 읽지 못함). `NG`나 `??`이면 종료 코드가 1입니다.

### 수동 로그인

비밀번호 자동 로그인이 캡차 등으로 막히면 터미널에서 직접 실행하세요(cron 불가).

```bash
./venv/bin/python naverpaper.py login 아이디
```

동작 순서는 다음과 같습니다.

1. 로그인 화면을 열고 **아이디/비밀번호를 자동으로 입력해 로그인을 시도**합니다. (캡차는 로그인을 시도한 뒤에야 나타나므로, 처음 열린 빈 로그인 화면에는 캡차가 없습니다.)
2. 캡차 없이 로그인되면 바로 쿠키를 저장하고 끝납니다.
3. 캡차가 나타나면 그 화면을 `data/login_아이디.png` 로 저장하고, **디스코드 웹훅이 설정되어 있으면 같은 이미지를 디스코드(스레드 포함)로 전송**합니다. 휴대폰의 디스코드에서 캡차 글자를 확인한 뒤 터미널에 입력하세요.
4. 틀리면 새 캡차 이미지가 다시 전송되며 최대 3회까지 시도합니다. 이미지에 캡차가 보이지 않으면 그냥 Enter(빈 입력)를 누르면 다시 로그인을 시도합니다. 디스코드 메시지의 `상태:` 줄에 마지막 결과가 표시됩니다.

- 웹훅은 보내기 전용이라 디스코드에서 답장을 써도 서버로 전달되지 않습니다. 글자는 터미널(또는 MCP의 `naverpaper_login_submit`)로 입력해야 합니다.
- 전송되는 이미지는 로그인 화면 캡처이므로 아이디 입력칸이 보일 수 있습니다. 개인용 스레드/채널에만 보내세요.
- MCP의 `naverpaper_login_start`/`naverpaper_login_submit` 도 같은 순서로 동작하고, 응답에 `discord_sent` 로 전송 여부를 알려 줍니다.

---

## 4. 적립 금액 확인

- 수집이 끝날 때마다 로그와 화면에 **이번 실행 적립**(계정별 `+N`)과 **계정별 오늘 / 7일 / 30일 / 전체 합계**가 출력됩니다.
- 언제든 `python3 naverpaper.py total` 로 다시 볼 수 있습니다.

```
== 적립 합계 (기록 시작: 2026-10-10, 총 3회 실행) ==
계정                          오늘        7일       30일          전체
idA                          7        27        27          37
idB                          3         3         3           8
합계                         10        30        30          45
```

### 적립액 계산 방식

적립액은 **이번 실행 직후 잔액 − 실행 직전 잔액**입니다. 직전 잔액을 잘못 읽으면 값이 크게 틀어집니다. 예를 들어 잔액이 로딩되기 전의 기본값 `0원`을 직전 잔액으로 읽으면, 지금까지 모은 전체 잔액이 **그 실행 한 번에 적립된 것**으로 기록됩니다. 이를 막기 위해 계정별로 **마지막으로 확인한 잔액**(`data/cookies/<아이디>.balance.json`)을 기준으로 삼습니다.

| 상황 | 처리 |
| --- | --- |
| 직전/직후 잔액이 정상 | `직후 − 직전` |
| 직전 잔액이 `0`, 직후는 양수, 기준 잔액 있음 | 직전 잔액을 `0`으로 잘못 읽은 것으로 보고 **기준 잔액을 직전 잔액으로** 계산 |
| 직전 잔액이 `0`, 직후는 양수, 기준 잔액 없음 (첫 측정) | **적립액을 세지 않고** 직후 잔액을 기준으로 저장 (로그에 `no baseline yet`, 디스코드에 `[참고]`) |
| 직전/직후를 못 읽음 | 적립액 0, `[참고]` 표시 |
| 포인트를 써서 잔액이 줄어듦 | 적립 0 (음수는 세지 않음) |
| 직후 잔액이 `0` | 로딩 값일 수 있어 기준 잔액으로 저장하지 않음 |

- 잔액이 늘었다는 것만 알 수 있어, 이 도구가 아닌 곳(앱 출석, 직접 클릭 등)의 적립이나 나중에 반영되는 적립은 정확히 구분하지 못합니다. 실제 내역은 네이버페이 포인트 내역이 기준입니다.
- `verify` 를 실행하면 현재 잔액이 기준 잔액으로 저장됩니다.
- 이미 잘못 기록된 값은 아래 `set-points` 로 고칠 수 있습니다.

```bash
./venv/bin/python naverpaper.py total -n 20            # 번호와 계정별 적립 확인
./venv/bin/python naverpaper.py set-points 7 아이디 0   # 7번 실행의 해당 계정 적립을 0으로 수정
```

수정 전에 DB를 `naverpaper.sqlite.bak-날짜시각` 으로 백업하고, 그 실행의 합계도 다시 계산합니다. 수집이 실행 중일 때는 거부합니다.

- 적립액은 수집 전후 네이버페이 포인트 **잔액 차이**로 계산한 값입니다. 수집 중 다른 곳에서 포인트를 쓰거나 받으면 오차가 생길 수 있고, 잔액을 읽지 못하면 0으로 기록됩니다.
- 합계는 `data/naverpaper.sqlite`에 쌓인 기록 기준이라, 기존 플러그인 DB를 복사해 오면 이전 기록까지 합산됩니다.

---

## 5. 디스코드 알림

`config.json`에 웹훅 주소를 넣으면 수집이 끝날 때마다 이번 적립과 누적 합계가 임베드 메시지로 전송됩니다. 수집 중 오류가 나면 빨간색으로 오류 내용이 전송됩니다.

```json
"discord_webhook_url": "https://discord.com/api/webhooks/웹훅ID/토큰?thread_id=스레드ID",
"discord_notify_mode": "always",
"discord_mask_ids": true
```

- 스레드로 보내려면 주소 끝에 `?thread_id=스레드ID`(숫자)를 그대로 붙입니다.
- 주소를 파일에 두기 싫으면 환경변수 `NAVERPAPER_DISCORD_WEBHOOK` 로도 지정할 수 있습니다.
- 먼저 `python3 naverpaper.py notify --test` 로 연결을 확인하세요.
- `python3 naverpaper.py notify` 는 수집 없이 현재 합계만 보냅니다. 하루 한 번 요약을 받고 싶다면 cron에 따로 등록하세요.
- 전송이 실패해도 수집 결과와 종료 코드에는 영향이 없습니다 (로그에 오류만 기록, 주소는 로그에 남기지 않음).

**보안 주의**: 웹훅 주소에는 토큰이 들어 있어 주소를 아는 사람은 누구나 해당 스레드에 글을 올릴 수 있습니다. `config.json` 권한을 600으로 두고 외부에 공유하지 마세요. 노출됐다면 디스코드에서 웹훅을 재생성하세요.

---

### 로그인 실패 알림 (원인 포함)

수집 중 네이버 로그인에 실패하거나 세션이 만료되어 처리하지 못한 계정이 있으면 디스코드로 빨간색 경고가 갑니다. 경고에는 **원인과 조치**가 함께 들어 있습니다. (`discord_notify_mode`가 `earned`여도 항상 전송됩니다.)

| 원인 | 판단 근거 | 조치 안내 |
| --- | --- | --- |
| 캡차 요구 | 실패한 로그인 화면에 자동입력 방지 문구 | `login 아이디` 실행 (캡차 이미지가 디스코드로 전송됨) |
| 아이디/비밀번호 불일치 | 화면의 "아이디 또는 비밀번호를 잘못 입력" 문구 | config.json 의 비밀번호 확인 |
| 추가 인증 요구 | 2단계 인증, 본인 확인, 새 환경 확인 문구 | PC 브라우저에서 인증을 마친 뒤 `login` |
| 계정 보호조치/이용 제한 | 보호조치, 이용 제한 문구 | 네이버에서 계정 상태 확인 |
| 휴면 계정 | 휴면 문구 | 네이버에서 휴면 해제 |
| 비밀번호 미설정 | `password empty` | config.json 에 비밀번호 입력 |
| 로그인 페이지 접속 시간 초과 | 로그인 페이지 열기 실패 | 서버 네트워크, 프록시, 부하 확인 |
| 로그인 화면 구조 변경 의심 | 아이디/비밀번호 입력칸을 못 찾음 | 로그의 `login diagnostic` 줄 확인 |
| 재로그인 건너뜀 | 최근 30분 안에 이미 재로그인을 시도함 | 수동 로그인 또는 30분 뒤 재시도 |
| 원인을 특정하지 못함 | 위 어느 문구에도 해당하지 않음 | 로그의 `login diagnostic - failed final_url=... body=...` 확인 |

- 원인 분류는 실패한 화면의 문구로 하기 때문에 **추정**입니다. 알림에는 `원문` 도 함께 표시되고, 화면 내용은 로그의 `login diagnostic - failed ... body=...` 줄에 남습니다.
- 수집할 새 링크가 없는 날도 쿠키를 쓸 수 없으면 수집 단계에서 로그인을 시도하므로, 로그인 문제를 놓치지 않고 알립니다.
- 같은 실행에서 이미 로그인에 실패했다면 다시 시도하지 않습니다(캡차 반복 방지).

### 실행 멈춤 / 비정상 종료 알림

| 알림 | 언제 | 비고 |
| --- | --- | --- |
| 진행이 멈춘 것 같습니다 | 수집 중 `stall_alert_minutes`(기본 15분) 동안 새 로그가 없음 | 최근 로그 8줄 포함. 느린 사이트 수집이면 일시적일 수 있음 |
| 진행이 다시 시작되었습니다 | 멈춤 알림 뒤 로그가 다시 올라옴 | 앞선 알림이 일시적 지연이었다는 안내 |
| 실행 시간이 오래 걸립니다 | 한 번의 실행이 `max_run_minutes`(기본 60분) 초과 | 실행당 한 번만 |
| 이전 실행이 끝나지 않아 건너뜀 | 이전 실행이 `max_run_minutes` 넘게 락을 잡고 있어 cron이 건너뜀 | PID, 프로세스 상태(정지됨 등), 종료 방법 안내. 1시간에 한 번만 |
| 이전 실행이 비정상 종료되었습니다 | 이전 실행이 정상 종료 없이 사라짐(서버 재부팅, 메모리 부족 강제 종료 등) | 다음 실행 시작 때 감지. 마지막으로 확인된 로그 포함 |
| 수집이 예외로 중단되었습니다 | 수집 중 처리되지 않은 예외 | 예외 내용과 스택 끝부분 포함 |

- 멈춘 프로세스가 락을 잡고 있으면 이후 cron이 모두 건너뛰므로, 알림이 오면 안내된 PID를 `kill` 하고 `pkill -f chrom` 으로 남은 브라우저를 정리하세요.
- "비정상 종료"는 프로세스가 갑자기 사라진 경우(`kill -9`, OOM)를 다음 실행에서 알아차리는 방식이라 **알림이 다음 실행 때 도착**합니다. 사용자가 Ctrl+C로 직접 중단한 경우나 예외로 이미 알린 경우는 비정상 종료로 보지 않습니다.
- MCP로 실행한 수집과 링크 적립도 같은 감시를 받습니다.
- 감시 상태는 `data/run_state.json` 에 30초마다 기록되고 정상 종료하면 지워집니다.
- 멈춤 알림만 끄려면 `"stall_alert_minutes": 0`, 시간 초과 알림은 `"max_run_minutes": 0` 으로 설정하세요. 둘 다 0이면 락 알림도 꺼집니다.

## 6. 데이터 위치

```
/mnt/naverpaper/
  naverpaper.py            실행 파일
  source_naverpaper.py     수집 엔진 (원본 그대로)
  config.json              설정 (git 에 올리지 않음)
  data/
    naverpaper.sqlite      수집·방문·적립 기록 DB
    cookies/<아이디>.json   로그인 쿠키
    cookies/<아이디>.relogin  마지막 자동 재로그인 시각 (30분 쿨다운 판단용)
    cookies/<아이디>.balance.json  마지막으로 확인한 포인트 잔액 (적립액 계산 기준)
    browser_profiles/      로그인용 브라우저 프로필
    naverpaper.log         로그
    run_state.json         실행 중 상태 (정상 종료하면 삭제, 남아 있으면 비정상 종료로 판단)
    lock_alert.marker      락 알림 중복 방지용
```

기존 플러그인 데이터를 이어 쓰려면 플러그인의 `data/` 폴더 내용(`naverpaper.sqlite`, `cookies/`, `browser_profiles/`)을 이 `data/`로 복사하세요.

`.gitignore` 에 `config.json`, `data/`, `venv/` 가 들어 있으니 비밀번호·쿠키·웹훅 토큰이 저장소에 올라가지 않습니다. 저장소가 공개라면 이 항목을 지우지 마세요.

---

## 7. MCP 서버 (LiteLLM 연결)

`naverpaper_mcp.py` 는 수집 기능을 MCP 도구로 노출합니다. 도구 이름은 모두 `naverpaper_` 로 시작합니다. 수집 엔진은 건드리지 않고 `naverpaper.py` 의 함수를 그대로 재사용합니다.

### 도구 목록

| 도구 | 설명 |
| --- | --- |
| `naverpaper_status` | 계정별 쿠키 파일 상태 (아이디 마스킹, `index` 번호 제공) |
| `naverpaper_verify` | 네이버에 실제 접속해 로그인 상태 확인 (`alive`/`expired`/`unknown`, 계정당 10~20초) |
| `naverpaper_totals` | 오늘/7일/30일/전체 적립 합계 + 최근 실행 내역 (`runs`: 1~50) |
| `naverpaper_run` | 수집을 **백그라운드로 시작**하고 즉시 반환 |
| `naverpaper_link` | 링크 하나 수동 적립 (백그라운드, 적립 가능한 https 리워드 링크만 허용) |
| `naverpaper_run_status` | 진행 중인 작업 상태와 결과 + DB에 기록된 마지막 실행 |
| `naverpaper_logs` | 로그 마지막 N줄 (1~300, 쿠키·웹훅·비밀번호는 가림) |
| `naverpaper_notify` | 적립 합계를 디스코드로 전송 (`test=true`: 테스트 메시지) |
| `naverpaper_login_start` | 캡차 수동 로그인 시작, **캡차 이미지를 함께 반환** (5분 유효) |
| `naverpaper_login_submit` | 캡차 글자 제출 (최대 3회) |

수집은 몇 분 걸릴 수 있어 `naverpaper_run` / `naverpaper_link` 는 바로 반환하고, 결과는 `naverpaper_run_status` 로 확인합니다. `account` 인자에는 `naverpaper_status` 의 `index` 번호나 마스킹된 아이디(`abc•••`)를 쓰면 됩니다.

### 설치

MCP용 패키지(`mcp`, `uvicorn`)도 `requirements.txt` 하나에 들어 있습니다. 이미 설치해 둔 환경이라면 다시 설치해서 추가하세요.

```bash
cd /mnt/naverpaper
source venv/bin/activate
pip install -r requirements.txt
```

### 토큰 설정

`config.json`에 토큰을 넣습니다. 이 파일은 비밀번호도 담고 있으므로 권한을 600으로 유지하세요.

```bash
openssl rand -hex 32        # 토큰 생성
nano /mnt/naverpaper/config.json
chmod 600 /mnt/naverpaper/config.json
```
```json
"mcp_token": "생성한-긴-랜덤-문자열",
```

토큰은 `--token` 옵션 > 환경변수 `NAVERPAPER_MCP_TOKEN` > `config.json`의 `mcp_token` 순서로 찾습니다. 설정 파일에 넣어 두면 별도의 환경 파일이 필요 없습니다.

### 실행

```bash
python3 naverpaper_mcp.py --host 0.0.0.0 --port 5934
```

| 옵션 | 설명 |
| --- | --- |
| `--transport` | `streamable-http`(기본) / `sse` / `stdio` |
| `--host`, `--port` | 기본 `127.0.0.1:5934`. 로컬이 아닌 주소에 바인딩하면 **토큰이 없을 때 시작을 거부**합니다 |
| `--token` | 인증 토큰 (위 우선순위 참고) |
| `--allowed-host` | Host 헤더 검사를 켜고 허용 호스트를 지정 (예: `192.168.0.10:5934`, 여러 번 지정 가능) |
| `-c`, `--config` | 설정 파일 경로 (환경변수 `NAVERPAPER_CONFIG`) |

인증은 `Authorization: Bearer <토큰>` 또는 `X-API-Key: <토큰>` 헤더를 받습니다. 접속 주소는 `http://호스트:5934/mcp` 입니다. 설정 파일을 고친 뒤에는 MCP 서버를 재시작해야 새 토큰이 적용됩니다.

### LiteLLM 설정 (`config.yaml`)

```yaml
mcp_servers:
  naverpaper:
    url: "http://서버IP:5934/mcp"
    transport: "http"
    auth_type: "bearer_token"
    auth_value: os.environ/NAVERPAPER_MCP_TOKEN
    description: "네이버 폐지줍기 관리"
```

- LiteLLM은 `naverpaper`의 `config.json`을 읽지 못하므로, **같은 토큰 값**을 LiteLLM 쪽에도 따로 넣어야 합니다. 환경변수 `NAVERPAPER_MCP_TOKEN`으로 넘기거나 `auth_value`에 값을 직접 적으세요. (이 환경변수는 LiteLLM 프로세스용이며 naverpaper 서버는 `config.json`에서 읽습니다.)
- LiteLLM이 도커 컨테이너라면 `서버IP` 자리에 호스트의 사설 IP(또는 `host.docker.internal`)를 쓰세요. `127.0.0.1` 은 컨테이너 자신을 가리킵니다.
- LiteLLM은 도구 이름 앞에 서버 이름(`naverpaper`)을 붙여 보여줄 수 있습니다. LiteLLM UI의 MCP Servers 화면에서 실제 노출 이름을 확인하세요.
- `421 Invalid Host header` 오류가 나면 `--allowed-host 서버IP:5934` 를 추가하세요.

### systemd 등록 (상시 실행)

`/etc/systemd/system/naverpaper-mcp.service`
```ini
[Unit]
Description=naverpaper MCP server
After=network-online.target

[Service]
WorkingDirectory=/mnt/naverpaper
ExecStart=/mnt/naverpaper/venv/bin/python /mnt/naverpaper/naverpaper_mcp.py --host 0.0.0.0 --port 5934
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

토큰은 `config.json`에서 읽으므로 환경 파일이 필요 없습니다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now naverpaper-mcp
journalctl -u naverpaper-mcp -f
```

### 보안 주의

- 비밀번호, 쿠키 값, 웹훅 주소는 어떤 도구도 돌려주지 않으며 로그 조회 시에도 가립니다. 계정 아이디는 마스킹됩니다.
- 이 서버는 수집 실행과 로그인을 시킬 수 있습니다. 방화벽(`ufw` 등)으로 5934 포트를 LiteLLM 서버 IP에서만 접근하도록 제한하고, 인터넷에 직접 노출하지 마세요.
- cron 수집과 MCP 실행은 같은 파일 락을 써서 겹치지 않습니다. 겹치면 MCP는 "건너뜀"으로 응답하고 cron은 종료 코드 2로 끝납니다.
- 캡차 로그인 중에는 락을 잡고 있어 수집이 시작되지 않습니다. 5분이 지나면 세션이 자동으로 닫히고 락이 풀립니다.

---

## 8. 원본(ff_naverpaper) 대비 엔진 변경 내역

`source_naverpaper.py`는 원본 코드를 그대로 쓰되 아래 부분만 수정했습니다. 원작자의 업데이트를 합칠 때 이 부분을 확인하세요.

- `AccountResult`: `session_expired`, `login_failed`, `relogin` 필드 추가
- 새 함수: `is_login_redirect`, `force_relogin`, `repair_expired_session`, `verify_session(_sync)` 와 세션 이벤트 도우미
- `process_account`, `process_account_manual_link`: 포인트 내역 페이지로 세션을 확인하고, 만료 시 재로그인 후 한 번 재시도. 만료 상태에서는 링크를 처리하지 않고 쿠키를 덮어쓰지 않음
- `classify_login_failure`, `password_login_on_page`: 로그인 실패 화면에서 원인(captcha 등)을 분류해 `AccountResult.login_error` 로 전달. `ensure_cookie_storage_state` 실패 사유 기록, 수집 단계에서 쿠키가 없으면 먼저 로그인 시도
- `_compute_estimated_points`, `load/save_last_balance`: 마지막 확인 잔액을 기준으로 적립액을 계산 (직전 잔액 0 오독으로 전체 잔액이 한 번에 적립되는 것 방지). `verify_session` 은 기준 잔액 저장과 포인트 내역 샘플 로그 추가
- `get_naverpay_point_balance`: 값이 나올 때까지 다시 읽고(0/실패 시 진단 로그), `AccountResult.balance_unreliable` 로 잔액을 못 믿는 경우를 표시
- `process_campaign_links`: 링크 처리 중 로그인 화면으로 이동하면 중단하고 방문 기록을 저장하지 않음
- `save_naver_campaign_urls`, `process_naverpay_mission_detail_source`, `process_naverpay_point_main`: 로그인 화면 이동 감지와 재로그인 후 미션 소스 재수집

## 9. 문제 해결

| 증상 | 확인 |
| --- | --- |
| `필수 패키지가 없습니다` | `source venv/bin/activate` 후 `pip install -r requirements.txt`, `playwright install chromium` |
| chromium 실행 오류, 공유 라이브러리 누락 | `playwright install-deps chromium` |
| 적립이 계속 0, 로그에 `nid.naver.com/nidlogin` 주소가 보임 | 세션 만료입니다. 이 버전은 자동 재로그인하지만 실패하면 디스코드로 경고합니다. `verify` 로 확인하고 `login 아이디` 로 수동 로그인 |
| `session expired; re-login skipped (tried within the last 30 min)` | 30분 이내에 재로그인을 이미 시도해 건너뜀. 수동 로그인하거나 30분 뒤 다음 실행을 기다리세요 |
| 로그인 로그에 `keep-login control not found` | 개편된 네이버 로그인 화면에서 "로그인 상태 유지"를 못 찾은 경우입니다. 이 버전은 문구 기반으로 다시 찾고, 그래도 못 찾으면 `keep-login candidates: [...]` 줄로 후보 요소의 HTML을 남깁니다. 그 줄을 알려 주시면 정확히 고칠 수 있습니다 |
| 로그인 성공 로그에 `session-only, no expiry date` | "로그인 상태 유지"가 적용되지 않아 세션 쿠키로 로그인된 것입니다. 동작은 하지만 세션이 일찍 끝날 수 있어, 만료되면 자동 재로그인합니다 |
| 적립 합계의 7일/30일/전체가 현재 잔액과 같고 오늘은 0 (실제로는 여러 날에 걸쳐 쌓인 잔액) | 직전 잔액을 `0`으로 잘못 읽어 전체 잔액이 한 번의 실행에 적립된 것으로 기록된 경우입니다. 이 버전부터는 기준 잔액으로 막고, 이미 기록된 값은 `total` 로 번호를 확인해 `set-points 번호 아이디 0` 으로 고치세요 |
| 로그에 `point balance 0 -> 0 (+0)`, 앱에서는 적립 알림이 왔음 | 잔액을 못 읽었거나 로딩 전 기본값 `0원`을 읽은 경우입니다. 이 버전은 값이 나올 때까지 최대 약 15초 다시 읽고, 끝까지 0이거나 못 읽으면 `point balance diagnostic - ... lines near '내 포인트'=[...]` 줄로 화면 내용을 남기며 디스코드 결과에 `[참고]` 줄을 붙입니다. 적립액이 정확하지 않다는 뜻이므로 네이버페이 포인트 내역으로 확인하세요. 진단 로그 줄을 알려 주시면 읽기 방식을 맞춰 고칠 수 있습니다 |
| `config.json`에 `네이버아이디2` 같은 예시 계정이 남아 있음 | `config.example.json`의 예시 계정입니다. 실제 계정이 아니면 삭제하세요. 가짜 계정으로 로그인을 시도하면 시간 낭비이고 네이버 보안에 걸릴 수 있으며 합계 표에도 0인 줄이 생깁니다 |
| 계정이 NG 로 나오고 로그인이 안 됨 | `python3 naverpaper.py login 아이디` 로 수동 로그인 |
| 종료 코드 2 | 이전 수집이 아직 실행 중이라 건너뜀. 정상 동작 |
| 디스코드로 안 옴 | `notify --test` 실행 후 로그의 HTTP 상태 확인 (404: 주소 또는 thread_id 오류, 401: 토큰 오류) |
| MCP: LiteLLM 에서 연결 실패 | 서버 주소/포트, 방화벽, 토큰 일치 여부 확인. 컨테이너라면 `127.0.0.1` 대신 호스트 IP 사용. `journalctl -u naverpaper-mcp` 확인 |
| MCP: `421 Invalid Host header` | `--allowed-host 서버IP:포트` 추가 |
| cron 에서만 실패 | cron 줄에서 `cd` 와 `./venv/bin/python` 경로 확인, `/mnt` 마운트 상태 확인 |

### 이전 버전에서 로그인이 풀린 채 처리된 링크 복구

이 버전으로 올리기 전, 로그인이 풀린 상태에서 처리된 링크는 적립 없이 방문 기록만 남아 있을 수 있습니다. 오늘 기록만 지우면 다시 시도됩니다. 수집이 실행 중이 아닐 때, 로그인 상태를 `verify`로 확인한 뒤 실행하세요.

```bash
cd /mnt/naverpaper
cp data/naverpaper.sqlite data/naverpaper.sqlite.bak
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect("data/naverpaper.sqlite")
n = c.execute("DELETE FROM url_visits WHERE user_id='아이디' AND visited_at >= date('now','localtime')").rowcount
c.commit(); print("삭제한 방문 기록:", n)
EOF
./venv/bin/python naverpaper.py
```

이미 적립받은 링크는 다시 방문해도 중복 적립되지 않고(시간만 더 걸림), 만료된 이벤트는 적립되지 않습니다.
