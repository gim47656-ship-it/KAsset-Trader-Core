# 🖥️ 호스트 환경 설정 가이드

Production 서버에서 Auto Trader를 배포하기 위한 호스트 환경 설정 방법입니다.

## 📋 사전 요구사항

- Ubuntu 20.04+ / CentOS 8+ / Rocky Linux 8+
- Docker & Docker Compose
- PostgreSQL (네이티브 설치)
- Redis (네이티브 설치)
- Git
- 기본 시스템 도구들

## 🐘 PostgreSQL 클라이언트 설치

### Ubuntu/Debian
```bash
# PostgreSQL 클라이언트 설치
sudo apt update
sudo apt install -y postgresql-client-17

# 연결 테스트
psql --version
```

### CentOS/RHEL/Rocky Linux
```bash
# PostgreSQL 클라이언트 설치
sudo dnf install -y postgresql

# 연결 테스트
psql --version
```

## 🐍 Python 환경 설정

### UV 사용 (권장)
```bash
# UV 설치
pip install uv

# 프로젝트 의존성 설치
cd /path/to/auto_trader
uv sync
```

### Virtual Environment 사용
```bash
# Python 3.11+ 설치 확인
python3 --version

# 가상환경 생성
python3 -m venv venv
source venv/bin/activate

# 의존성 설치
pip install -r requirements.txt  # 또는 pip install .
```

## 🔧 환경 변수 설정

### 1. Production 환경 파일 생성
```bash
# 템플릿 복사
cp env.prod.example .env.prod

# 환경변수 편집
nano .env.prod
```

### 2. 필수 설정 항목
```bash
# 데이터베이스 연결
DATABASE_URL=postgresql+asyncpg://auto_trader:password@localhost:5432/auto_trader_prod

# Redis 연결
REDIS_URL=redis://:password@localhost:6379/0

# GitHub 저장소 (Docker 이미지용)
GITHUB_REPOSITORY=your-username/auto_trader

# API 키들 — 필요한 항목은 env.example / env.prod.example을 기준으로 채웁니다.
OPENDART_API_KEY=your_opendart_key
# ... 기타 필요한 키들
```

## 🗂️ 프로젝트 구조 설정

### 1. 프로젝트 클론
```bash
# 프로덕션 전용 사용자 생성
sudo useradd -m -s /bin/bash autotrader
sudo usermod -aG docker autotrader

# 프로젝트 클론
sudo -u autotrader git clone https://github.com/your-username/auto_trader.git /home/autotrader/auto_trader
cd /home/autotrader/auto_trader
git checkout production
```

### 2. 디렉토리 권한 설정
```bash
# 필요한 디렉토리 생성
sudo -u autotrader mkdir -p /home/autotrader/auto_trader/{tmp,logs}
sudo -u autotrader chmod 755 /home/autotrader/auto_trader/{tmp,logs}

# 스크립트 실행 권한
sudo -u autotrader chmod +x /home/autotrader/auto_trader/scripts/*.sh
```

## 🔑 권한 및 보안 설정

### 1. 데이터베이스 접근 권한
```bash
# .pgpass 파일 생성 (패스워드 없이 접근)
sudo -u autotrader tee /home/autotrader/.pgpass > /dev/null <<EOF
localhost:5432:auto_trader_prod:auto_trader:your_password
EOF

sudo -u autotrader chmod 600 /home/autotrader/.pgpass
```

### 2. 환경변수 파일 보안
```bash
# .env.prod 파일 권한 설정
sudo -u autotrader chmod 600 /home/autotrader/auto_trader/.env.prod
```

## 🧪 환경 테스트

### 1. 데이터베이스 연결 테스트
```bash
# 호스트에서 직접 테스트
sudo -u autotrader psql "postgresql://auto_trader:password@localhost:5432/auto_trader_prod" -c "SELECT version();"
```

### 2. Python 환경 테스트
```bash
# UV 환경에서 테스트
cd /home/autotrader/auto_trader
sudo -u autotrader uv run python -c "import asyncpg, redis; print('✅ Dependencies OK')"

# 또는 가상환경에서 테스트
sudo -u autotrader bash -c "cd /home/autotrader/auto_trader && source venv/bin/activate && python -c 'import asyncpg, redis; print(\"✅ Dependencies OK\")'"
```

### 3. 마이그레이션 테스트
```bash
# 마이그레이션 체크 스크립트 실행
cd /home/autotrader/auto_trader
sudo -u autotrader ./scripts/migration-check.sh
```

## 🚀 배포 스크립트 사용법

### 1. 기본 배포 (호스트 기반 마이그레이션)
```bash
cd /home/autotrader/auto_trader
sudo -u autotrader ./scripts/deploy.sh --manual-migrate --backup
```

### 2. 호스트 기반 마이그레이션만 실행
```bash
sudo -u autotrader ./scripts/migrate.sh
```

### 3. 자동 배포 (낮은 위험도)
```bash
sudo -u autotrader ./scripts/deploy.sh --auto-migrate --health-check
```

## 🔄 시스템 서비스 설정

### 1. Systemd 서비스 파일 생성
```bash
sudo tee /etc/systemd/system/auto-trader.service > /dev/null <<EOF
[Unit]
Description=Auto Trader Application
Requires=docker.service postgresql.service redis.service
After=docker.service postgresql.service redis.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=autotrader
WorkingDirectory=/home/autotrader/auto_trader
ExecStart=/usr/local/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/local/bin/docker compose -f docker-compose.prod.yml down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
```

### 2. 서비스 활성화
```bash
sudo systemctl enable auto-trader.service
sudo systemctl start auto-trader.service

# 상태 확인
sudo systemctl status auto-trader.service
```

## 📊 모니터링 설정

### 1. 로그 로테이션
```bash
sudo tee /etc/logrotate.d/auto-trader > /dev/null <<EOF
/home/autotrader/auto_trader/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 autotrader autotrader
}
EOF
```

### 2. 헬스체크 Cron 설정
```bash
# autotrader 사용자의 crontab 설정
sudo -u autotrader crontab -e

# 다음 라인 추가 (5분마다 헬스체크)
*/5 * * * * cd /home/autotrader/auto_trader && ./scripts/healthcheck.sh >> /home/autotrader/auto_trader/logs/healthcheck.log 2>&1
```

## 🔧 문제 해결

### 1. PostgreSQL 연결 문제
```bash
# 연결 확인
sudo -u autotrader psql "postgresql://auto_trader:password@localhost:5432/auto_trader_prod" -c "\l"

# 서비스 상태 확인
sudo systemctl status postgresql
```

### 2. Python 의존성 문제
```bash
# UV 환경 재설치
cd /home/autotrader/auto_trader
sudo -u autotrader uv sync

# 또는 가상환경 재생성
sudo -u autotrader rm -rf venv
sudo -u autotrader python3 -m venv venv
sudo -u autotrader bash -c "source venv/bin/activate && pip install -r requirements.txt"
```

### 3. 권한 문제
```bash
# 디렉토리 권한 재설정
sudo chown -R autotrader:autotrader /home/autotrader/auto_trader
sudo chmod 755 /home/autotrader/auto_trader/{tmp,logs}
sudo chmod 600 /home/autotrader/auto_trader/.env.prod
```

## 📈 성능 최적화

### 1. PostgreSQL 튜닝
```bash
# PostgreSQL 설정 확인
sudo -u postgres psql -c "SHOW shared_buffers;"
sudo -u postgres psql -c "SHOW work_mem;"
```

### 2. 시스템 리소스 모니터링
```bash
# 리소스 사용량 확인
htop
df -h
free -h

# Docker 컨테이너 리소스 확인
docker stats
```

이제 호스트 기반 환경에서 안전하고 효율적으로 Auto Trader를 운영할 수 있습니다! 🚀


