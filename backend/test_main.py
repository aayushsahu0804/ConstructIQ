import base64
import io
import json
import logging
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from main import app
import main
import config
from config import BACKEND_DIR, ENV_FILE, gemini_api_key, gemini_attempt_timeout_seconds, gemini_fallback_model, gemini_timeout_seconds, load_environment

client = TestClient(app)

def test_health():
    assert client.get('/api/health').json()['status'] == 'healthy'

def test_plan_returns_estimates_and_tasks():
    response = client.post('/api/plan', json={'area_sqft':1000,'location':'Tier 1','building_type':'Residential','completion':25,'budget':2500000,'materials':[]})
    assert response.status_code == 200
    data = response.json()
    assert data['estimated_total_inr'] > 0
    assert data['provider'] == 'Deterministic estimate'
    assert len(data['tasks']) >= 3
    assert any('not a professional' in x for x in data['assumptions'])

def test_plan_rejects_invalid_completion():
    assert client.post('/api/plan', json={'area_sqft':1000,'completion':101,'budget':1}).status_code == 422

def test_image_analysis_does_not_fake_without_key(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    response = client.post('/api/analyze?mode=safety', files={'image':('site.jpg',b'img','image/jpeg')})
    assert response.status_code == 503

def test_dotenv_load_is_backend_relative_and_shared_with_health(tmp_path, monkeypatch):
    # A synthetic test value verifies loading without reading or displaying the real key.
    dotenv_file = tmp_path / '.env'
    dotenv_file.write_text('GEMINI_API_KEY=test-only-placeholder\n', encoding='utf-8')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'ENV_FILE', dotenv_file)
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '')

    assert BACKEND_DIR == ENV_FILE.parent
    assert ENV_FILE.is_absolute()
    load_environment()

    assert bool(gemini_api_key()) is True
    assert client.get('/api/health').json()['ai_configured'] is True

def test_gemini_request_uses_supported_model_auth_and_image_payload(monkeypatch):
    secret = 'test-only-key-do-not-log'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    observed = {}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}]}'

    def fake_urlopen(request, timeout):
        observed['url'] = request.full_url
        observed['auth'] = request.get_header('X-goog-api-key')
        observed['payload'] = json.loads(request.data)
        observed['timeout'] = timeout
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    assert main.gemini('inspect image', b'image-bytes', 'image/png') == '{}'
    assert observed['url'] == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent'
    assert '?' not in observed['url']
    assert observed['auth'] == secret
    parts = observed['payload']['contents'][0]['parts']
    assert parts[1]['inline_data'] == {'mime_type': 'image/png', 'data': base64.b64encode(b'image-bytes').decode()}
    assert observed['payload']['generationConfig'] == {'responseMimeType': 'application/json'}
    assert observed['timeout'] == 30

def test_gemini_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', '72.5')
    assert gemini_timeout_seconds() == 72.5
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', '2')
    assert gemini_timeout_seconds() == 15
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', 'not-a-number')
    assert gemini_timeout_seconds() == 90

def test_gemini_attempt_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv('GEMINI_ATTEMPT_TIMEOUT_SECONDS', '22.5')
    assert gemini_attempt_timeout_seconds() == 22.5
    monkeypatch.setenv('GEMINI_ATTEMPT_TIMEOUT_SECONDS', '2')
    assert gemini_attempt_timeout_seconds() == 5
    monkeypatch.setenv('GEMINI_ATTEMPT_TIMEOUT_SECONDS', '100')
    assert gemini_attempt_timeout_seconds() == 60

def test_gemini_models_have_configurable_defaults(monkeypatch):
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.delenv('GEMINI_FALLBACK_MODEL', raising=False)
    assert main.gemini_model() == 'gemini-3.8-flash'
    assert gemini_fallback_model() == 'gemini-3.6-flash'
    monkeypatch.setenv('GEMINI_MODEL', 'primary-test-model')
    monkeypatch.setenv('GEMINI_FALLBACK_MODEL', 'fallback-test-model')
    assert main.gemini_model() == 'primary-test-model'
    assert gemini_fallback_model() == 'fallback-test-model'

def _http_error(request, status, message='temporary provider outage'):
    body = json.dumps({'error': {'code': status, 'status': 'UNAVAILABLE', 'message': message}}).encode()
    return HTTPError(request.full_url, status, 'Provider error', None, io.BytesIO(body))

def _gemini_response_text(text):
    return json.dumps({'candidates': [{'content': {'parts': [{'text': text}]}}]}).encode()

def test_gemini_retries_transient_errors_then_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main.time, 'sleep', lambda _delay: None)
    monkeypatch.setattr(main.random, 'uniform', lambda _low, _high: 0)
    attempts = []

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return _gemini_response_text('real-shaped response')

    def fake_urlopen(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) <= 2:
            raise _http_error(request, [500, 503][len(attempts) - 1])
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        assert main.gemini('prompt') == 'real-shaped response'

    assert len(attempts) == 3
    assert all('gemini-3.8-flash' in url for url in attempts)
    assert 'status=500 retry_count=0' in caplog.text
    assert 'status=503 retry_count=1' in caplog.text

def test_gemini_falls_back_after_primary_503_and_validates_schema(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main.time, 'sleep', lambda _delay: None)
    monkeypatch.setattr(main.random, 'uniform', lambda _low, _high: 0)
    calls = {'gemini-3.8-flash': 0, 'gemini-3.6-flash': 0}
    result = {'summary': 'Visible observation only', 'findings': [{'title': 'Missing visible hard hat', 'detail': 'One worker appears without head protection.', 'severity': 'medium', 'confidence': 0.72, 'action': 'Verify PPE on site and provide required head protection.'}], 'limitations': []}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return _gemini_response_text(json.dumps(result))

    def fake_urlopen(request, timeout):
        model = 'gemini-3.8-flash' if 'gemini-3.8-flash' in request.full_url else 'gemini-3.6-flash'
        calls[model] += 1
        if model == 'gemini-3.8-flash':
            raise _http_error(request, 503, 'UNAVAILABLE')
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    assert response.json()['provider'] == 'Gemini'
    assert response.json()['findings'][0]['title'] == result['findings'][0]['title']
    assert response.json()['mode'] == 'safety'
    assert calls == {'gemini-3.8-flash': 3, 'gemini-3.6-flash': 1}
    assert 'Gemini fallback starting: model=gemini-3.6-flash reason=primary HTTP 503 after retries' in caplog.text

def test_gemini_fails_when_primary_and_fallback_are_unavailable(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main.time, 'sleep', lambda _delay: None)
    monkeypatch.setattr(main.random, 'uniform', lambda _low, _high: 0)
    calls = {'gemini-3.8-flash': 0, 'gemini-3.6-flash': 0}

    def fake_urlopen(request, timeout):
        model = 'gemini-3.8-flash' if 'gemini-3.8-flash' in request.full_url else 'gemini-3.6-flash'
        calls[model] += 1
        raise _http_error(request, 503, 'UNAVAILABLE')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert calls == {'gemini-3.8-flash': 3, 'gemini-3.6-flash': 3}

@pytest.mark.parametrize('status', [400, 401, 403, 429])
def test_gemini_does_not_retry_or_fallback_for_nontransient_http_errors(monkeypatch, status):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request, status, 'request rejected')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert len(calls) == 1
    assert 'gemini-3.8-flash' in calls[0]

def test_analyze_timeout_returns_generic_error_and_logs_safe_timeout(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    image_bytes = b'private-image-content'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', '67')
    observed = []

    def fake_urlopen(request, timeout):
        observed.append(timeout)
        raise TimeoutError(f'{secret} https://example.test/?key={secret} {image_bytes!r}')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', image_bytes, 'image/png')})

    assert observed == [30, 30]
    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'model=gemini-3.8-flash attempt=1 timeout_seconds=30.00 reason=provider socket timeout' in caplog.text
    assert 'model=gemini-3.6-flash attempt=1 timeout_seconds=30.00' in caplog.text
    assert secret not in caplog.text
    assert 'example.test' not in caplog.text
    assert image_bytes.decode() not in caplog.text

def test_primary_timeout_uses_fallback_and_returns_valid_analysis(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main, 'gemini_timeout_seconds', lambda: 60)
    monkeypatch.setattr(main, 'gemini_attempt_timeout_seconds', lambda: 30)
    result = {'summary': 'Visible observation', 'findings': [], 'limitations': []}
    calls = []

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return _gemini_response_text(json.dumps(result))

    def fake_urlopen(request, timeout):
        calls.append(('gemini-3.8-flash' if 'gemini-3.8-flash' in request.full_url else 'gemini-3.6-flash', timeout))
        if len(calls) == 1:
            raise TimeoutError('provider socket timeout')
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    assert response.json()['summary'] == result['summary']
    assert [model for model, _timeout in calls] == ['gemini-3.8-flash', 'gemini-3.6-flash']
    assert calls[0][1] == 30
    assert calls[1][1] <= 30
    assert 'model=gemini-3.8-flash attempt=1 timeout_seconds=30.00 reason=provider socket timeout' in caplog.text
    assert 'model=gemini-3.6-flash reason=primary timeout' in caplog.text

def test_both_models_timeout_return_generic_error(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main, 'gemini_timeout_seconds', lambda: 60)
    monkeypatch.setattr(main, 'gemini_attempt_timeout_seconds', lambda: 30)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError('provider socket timeout')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert len(calls) == 2
    assert 'model=gemini-3.8-flash attempt=1 timeout_seconds=30.00' in caplog.text
    assert 'model=gemini-3.6-flash attempt=1 timeout_seconds=30.00' in caplog.text

def test_primary_timeout_skips_fallback_when_overall_budget_is_exhausted(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    monkeypatch.setattr(main, 'gemini_fallback_model', lambda: 'gemini-3.6-flash')
    monkeypatch.setattr(main, 'gemini_timeout_seconds', lambda: 20)
    monkeypatch.setattr(main, 'gemini_attempt_timeout_seconds', lambda: 5)
    now = [0.0]
    calls = []

    def fake_monotonic(): return now[0]
    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        now[0] = 18.0  # A slow request leaves less than the five seconds needed for fallback.
        raise TimeoutError('provider socket timeout')

    monkeypatch.setattr(main.time, 'monotonic', fake_monotonic)
    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert len(calls) == 1
    assert 'fallback skipped' in caplog.text
    assert 'insufficient overall budget' in caplog.text

def test_gemini_http_diagnostic_is_sanitized(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    body = json.dumps({'error': {'status': 'INVALID_ARGUMENT', 'message': f'Unsupported parameter; api_key={secret}'}}).encode()

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 400, 'Bad Request', None, io.BytesIO(body))

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        try:
            main.gemini('inspect image')
        except HTTPError:
            pass
        else:
            raise AssertionError('Expected Gemini HTTP error')

    assert 'status=400' in caplog.text
    assert 'INVALID_ARGUMENT' in caplog.text
    assert 'Unsupported parameter' in caplog.text
    assert secret not in caplog.text
    assert '?key=' not in caplog.text

def test_analyze_http_error_logs_sanitized_provider_body(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    image_bytes = b'private-image-content'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')
    body = json.dumps({
        'error': {
            'status': 'INVALID_ARGUMENT',
            'message': f'Invalid parameter; api_key={secret}; Authorization: Bearer {secret}; https://example.test/?key={secret}',
        },
        'contents': [{'parts': [{'inline_data': {'mime_type': 'image/png', 'data': image_bytes.decode()}}]}],
        'diagnostic_echo': base64.b64encode(image_bytes).decode(),
    }).encode()

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 400, 'Bad Request', None, io.BytesIO(body))

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', image_bytes, 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'status=400' in caplog.text
    assert 'INVALID_ARGUMENT' in caplog.text
    assert 'Invalid parameter' in caplog.text
    assert secret not in caplog.text
    assert 'example.test' not in caplog.text
    assert image_bytes.decode() not in caplog.text
    assert base64.b64encode(image_bytes).decode() not in caplog.text

def test_analyze_non_http_failure_logs_safe_type_and_message(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    private_image = 'private-image-content'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.8-flash')

    def fake_urlopen(request, timeout):
        raise ValueError(f'{secret} {private_image} https://example.test/?key={secret}')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'private-image-content', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'exception_type=ValueError' in caplog.text
    assert 'safe_message=invalid provider response structure' in caplog.text
    assert secret not in caplog.text
    assert private_image not in caplog.text
    assert 'example.test' not in caplog.text
