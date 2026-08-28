"""이 디렉터리를 패키지로 만들어 테스트 모듈 이름 충돌을 없앤다.

`tests/brokers/kis/mock_scalping_ws/` 에도 `test_candles.py` 와
`test_market_stream.py` 가 있다. pytest 기본 import 모드(prepend)는 파일에서
위로 올라가며 `__init__.py` 가 있는 동안만 패키지 경로를 만들고, 없으면 파일
이름만으로 모듈을 import 한다. 그래서 이 파일이 없으면 두 디렉터리의 같은
이름 파일이 같은 모듈명으로 충돌해 `import file mismatch` 로 수집 자체가
실패한다.

수집이 실패하면 단순히 그 파일만 빠지는 게 아니다. CI 의 shard 매니페스트는
`pytest --collect-only` 결과를 기준 집합으로 쓰므로(`ci_shards/`,
`scripts/ci/file_shard_plan.py`), 수집되지 않은 파일은 매니페스트에 들어가지
못하고 CI 에서 영구히 실행되지 않는다. `tests/services/brokers/toss/` 가 같은
이유로 이미 이 파일을 두고 있다.
"""
