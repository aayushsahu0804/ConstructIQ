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
from config import BACKEND_DIR, ENV_FILE, gemini_api_key, gemini_attempt_timeout_seconds, gemini_timeout_seconds, load_environment

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

@pytest.mark.parametrize('area', [0, -1, 10000001])
def test_plan_rejects_invalid_or_extreme_area(area):
    assert client.post('/api/plan', json={'area_sqft': area}).status_code == 422

def test_plan_has_editable_property_and_structure_inputs_and_no_budget_variance():
    response = client.post('/api/plan', json={
        'area_sqft': 1000, 'location': 'Tier 1', 'property_tier': 'Premium',
        'building_type': 'Residential house', 'budget': 1,
    })
    assert response.status_code == 200
    data = response.json()
    assert data['estimated_total_inr'] == 2690400
    assert data['provider'] == 'Deterministic estimate'
    assert 'budget_variance_inr' not in data
    assert 'budget_inr' not in data
    assert any('City Tier 1 factor: 1.18' in line for line in data['assumptions'])
    assert any('Property Premium factor: 1.20' in line for line in data['assumptions'])
    assert 'not a professional valuation' in data['assumptions'][-1]

@pytest.mark.parametrize('structure', [
    'Residential house', 'Apartment building', 'Commercial building',
    'Warehouse / industrial', 'Renovation',
])
def test_plan_supports_each_structure_type(structure):
    response = client.post('/api/plan', json={'area_sqft': 500, 'building_type': structure, 'property_tier': 'Value'})
    assert response.status_code == 200
    assert response.json()['estimated_total_inr'] > 0

def test_plan_rejects_invalid_tiers_and_extreme_material_inputs():
    base = {'area_sqft': 1000}
    assert client.post('/api/plan', json={**base, 'location': 'Tier 4'}).status_code == 422
    assert client.post('/api/plan', json={**base, 'property_tier': 'Ultra'}).status_code == 422
    assert client.post('/api/plan', json={**base, 'materials': [{'name':'Cement','quantity':100000001,'unit':'bags','rate':1}]}).status_code == 422

class FakeWeatherResponse:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()

def test_weather_location_geocodes_and_returns_real_provider_fields(monkeypatch):
    responses = [
        {'results': [{'name': 'Pune', 'admin1': 'Maharashtra', 'country': 'India', 'latitude': 18.52, 'longitude': 73.86}]},
        {
            'current': {'time': '2026-10-24T12:00', 'temperature_2m': 29.1, 'precipitation': 0.2, 'wind_speed_10m': 11.4, 'weather_code': 2},
            'daily': {'time': ['2026-10-24', '2026-10-25', '2026-10-26'], 'temperature_2m_min': [20, 21, 19], 'temperature_2m_max': [30, 31, 29], 'precipitation_probability_max': [10, 20, 5], 'weather_code': [2, 61, 0]},
        },
    ]
    urls = []
    def fake_urlopen(url, timeout):
        urls.append(str(url))
        assert timeout == 8
        return FakeWeatherResponse(responses.pop(0))
    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)

    response = client.get('/api/weather', params={'location': 'Pune'})

    assert response.status_code == 200
    data = response.json()
    assert data['available'] is True
    assert data['location'] == 'Pune, Maharashtra, India'
    assert data['current']['temperature_c'] == 29.1
    assert data['current']['condition'] == 'Partly cloudy'
    assert data['forecast'][1]['condition'] == 'Light rain'
    assert data['forecast'][1]['precipitation_probability_percent'] == 20
    assert 'geocoding-api.open-meteo.com' in urls[0]
    assert 'api.open-meteo.com/v1/forecast' in urls[1]

def test_weather_invalid_location_returns_clear_not_found(monkeypatch):
    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeWeatherResponse({'results': []}))
    response = client.get('/api/weather', params={'location': 'No Such Place'})
    assert response.status_code == 404
    assert response.json()['detail'] == 'Location not found. Check the spelling and try again.'

def test_weather_provider_failure_is_explicit_and_does_not_invent_conditions(monkeypatch):
    calls = [0]
    def fake_urlopen(_url, timeout):
        calls[0] += 1
        if calls[0] == 1:
            return FakeWeatherResponse({'results': [{'name': 'Pune', 'latitude': 18.52, 'longitude': 73.86}]})
        raise OSError('provider offline')
    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.get('/api/weather', params={'location': 'Pune'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'Live weather provider is unavailable. Try again shortly.'

def test_weather_geocoding_timeout_is_distinct_and_logged(monkeypatch, caplog):
    def timeout(*_args, **_kwargs):
        raise TimeoutError('provider timed out')
    monkeypatch.setattr(main.urllib.request, 'urlopen', timeout)
    response = client.get('/api/weather', params={'location': 'Pune'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'Weather location lookup timed out. Try again shortly.'
    assert 'stage=geocoding kind=timeout' in caplog.text

def test_weather_http_error_logs_status_and_sanitizes_body(monkeypatch, caplog):
    responses = [
        FakeWeatherResponse({'results': [{'name': 'Pune', 'latitude': 18.52, 'longitude': 73.86}]}),
    ]
    def provider(url, timeout):
        if responses:
            return responses.pop()
        raise HTTPError('https://api.open-meteo.com/?api_key=never-log-this', 503, 'UNAVAILABLE', {}, io.BytesIO(b'UNAVAILABLE api_key=never-log-this'))
    monkeypatch.setattr(main.urllib.request, 'urlopen', provider)
    response = client.get('/api/weather', params={'location': 'Pune'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'Live weather provider is unavailable. Try again shortly.'
    assert 'stage=forecast kind=http_error status=503' in caplog.text
    assert 'never-log-this' not in caplog.text
    assert 'api.open-meteo.com' not in caplog.text

def test_weather_malformed_forecast_is_logged_and_returned_safely(monkeypatch, caplog):
    responses = [
        FakeWeatherResponse({'results': [{'name': 'Pune', 'latitude': 18.52, 'longitude': 73.86}]}),
        FakeWeatherResponse({'current': {}, 'daily': {'time': []}}),
    ]
    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: responses.pop(0))
    response = client.get('/api/weather', params={'location': 'Pune'})
    assert response.status_code == 502
    assert response.json()['detail'] == 'Weather provider returned an invalid response. Try again shortly.'
    assert 'stage=forecast kind=malformed_response' in caplog.text

def test_weather_requires_coordinate_pair_and_keeps_no_location_empty(monkeypatch):
    assert client.get('/api/weather', params={'latitude': 18.52}).status_code == 422
    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: pytest.fail('No provider request expected'))
    response = client.get('/api/weather')
    assert response.status_code == 200
    assert response.json()['available'] is False
    assert response.json()['forecast'] == []

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
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    observed = {'calls': 0}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}]}'

    def fake_urlopen(request, timeout):
        observed['calls'] += 1
        observed['url'] = request.full_url
        observed['auth'] = request.get_header('X-goog-api-key')
        observed['payload'] = json.loads(request.data)
        observed['timeout'] = timeout
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    assert main.gemini('inspect image', b'image-bytes', 'image/png') == '{}'
    assert observed['url'] == 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
    assert '?' not in observed['url']
    assert observed['auth'] == secret
    parts = observed['payload']['contents'][0]['parts']
    assert parts[1]['inline_data'] == {'mime_type': 'image/png', 'data': base64.b64encode(b'image-bytes').decode()}
    assert observed['payload']['generationConfig'] == {
        'responseFormat': {
            'text': {
                'mimeType': 'APPLICATION_JSON',
                'schema': main.GEMINI_IMAGE_ANALYSIS_SCHEMA,
            },
        },
    }
    assert observed['timeout'] == 30
    assert observed['calls'] == 1  # Primary success does not invoke fallback.

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

def test_gemini_model_has_configurable_default(monkeypatch):
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    assert main.gemini_model() == 'gemini-3.5-flash-lite'
    monkeypatch.setenv('GEMINI_MODEL', 'configured-test-model')
    assert main.gemini_model() == 'configured-test-model'

def _http_error(request, status, message='temporary provider outage'):
    body = json.dumps({'error': {'code': status, 'status': 'UNAVAILABLE', 'message': message}}).encode()
    return HTTPError(request.full_url, status, 'Provider error', None, io.BytesIO(body))

def _gemini_response_text(text):
    return json.dumps({'candidates': [{'content': {'parts': [{'text': text}]}}]}).encode()

def test_gemini_retries_transient_errors_then_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
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
    assert all('gemini-3.5-flash-lite' in url for url in attempts)
    assert 'status=500 retry_count=0' in caplog.text
    assert 'status=503 retry_count=1' in caplog.text

def test_analyze_parses_standard_gemini_rest_response_parts_with_selected_model(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    result = {
        'summary': 'Visible observation only',
        'findings': [{
            'title': 'Missing visible hard hat',
            'detail': 'A worker appears without head protection.',
            'severity': 'medium',
            'confidence': 0.72,
            'action': 'Verify PPE on site and provide required head protection.',
        }],
        'limitations': ['Visual observations only.'],
    }
    calls = []

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{
                    'content': {
                        'role': 'model',
                        'parts': [
                            {
                                'text': json.dumps({'internal_note': 'not the final report'}),
                                'thought': True,
                                'thoughtSignature': 'test-only-signature',
                            },
                            {
                                'text': json.dumps(result),
                                'thoughtSignature': 'test-only-signature',
                            },
                        ],
                    },
                    'finishReason': 'STOP',
                }],
                'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 30},
            }).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        payload = json.loads(request.data)
        assert request.full_url.endswith('/models/gemini-3.5-flash-lite:generateContent')
        assert payload['generationConfig']['responseFormat']['text'] == {
            'mimeType': 'APPLICATION_JSON',
            'schema': main.GEMINI_IMAGE_ANALYSIS_SCHEMA,
        }
        image_part = payload['contents'][0]['parts'][1]['inline_data']
        assert image_part == {
            'mime_type': 'image/png',
            'data': base64.b64encode(b'site-image').decode(),
        }
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'site-image', 'image/png')})

    assert response.status_code == 200
    assert response.json()['provider'] == 'Gemini'
    assert response.json()['mode'] == 'safety'
    assert response.json()['summary'] == result['summary']
    assert response.json()['findings'][0]['severity'] == 'medium'
    assert len(calls) == 1


def test_analyze_rejects_provider_json_that_violates_response_schema(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    invalid_result = {
        'summary': 'Potential hazard observed',
        'findings': [{
            'title': 'Unprotected edge', 'detail': 'An edge appears unguarded.',
            'severity': 'critical', 'confidence': 0.8,
            'action': 'Restrict access and arrange inspection.',
        }],
        'limitations': [],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            text = json.dumps(invalid_result)
            return _gemini_response_text(text)

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'exception_type=ValidationError' in caplog.text
    assert 'response schema validation failed (findings.0.severity:literal_error)' in caplog.text
    assert 'critical' not in caplog.text

def test_gemini_quota_failure_does_not_call_another_model(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request, 429, 'RESOURCE_EXHAUSTED')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=crack', files={'image': ('crack.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert len(calls) == 1
    assert calls[0].endswith('/models/gemini-3.5-flash-lite:generateContent')
    assert 'status=429' in caplog.text

@pytest.mark.parametrize('status', [400, 401, 403])
def test_gemini_does_not_retry_nontransient_http_errors(monkeypatch, status):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request, status, 'request rejected')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert len(calls) == 1
    assert 'gemini-3.5-flash-lite' in calls[0]

def test_analyze_timeout_returns_generic_error_and_logs_safe_timeout(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    image_bytes = b'private-image-content'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', '67')
    observed = []

    def fake_urlopen(request, timeout):
        observed.append(timeout)
        raise TimeoutError(f'{secret} https://example.test/?key={secret} {image_bytes!r}')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', image_bytes, 'image/png')})

    assert observed == [30]
    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'model=gemini-3.5-flash-lite attempt=1 timeout_seconds=30.00 reason=provider socket timeout' in caplog.text
    assert secret not in caplog.text
    assert 'example.test' not in caplog.text
    assert image_bytes.decode() not in caplog.text

def test_primary_timeout_returns_safe_error_without_selecting_another_model(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError('provider socket timeout')

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert calls == ['https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent']
    assert 'model=gemini-3.5-flash-lite attempt=1 timeout_seconds=' in caplog.text
    assert 'fallback' not in caplog.text.lower()

def test_gemini_http_diagnostic_is_sanitized(monkeypatch, caplog):
    secret = 'test-only-key-do-not-log'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
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
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
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
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

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

def test_gemini_model_normalizes_stale_gemini_38_flash(monkeypatch):
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-3.8-flash')
    assert main.gemini_model() == 'gemini-3.5-flash-lite'
    monkeypatch.setenv('GEMINI_MODEL', '  gemini-3.8-flash  ')
    assert main.gemini_model() == 'gemini-3.5-flash-lite'
    monkeypatch.setenv('GEMINI_MODEL', '')
    assert main.gemini_model() == 'gemini-3.5-flash-lite'

def test_analyze_parses_markdown_wrapped_json_response(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    result = {
        'summary': 'Fenced JSON response',
        'findings': [{
            'title': 'Trip hazard',
            'detail': 'Loose cables on walkway.',
            'severity': 'low',
            'confidence': 0.85,
            'action': 'Route cables overhead or secure with cable protectors.',
        }],
        'limitations': ['Visual observations only.'],
    }
    fenced_text = f"```json\n{json.dumps(result)}\n```"

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{
                    'content': {
                        'role': 'model',
                        'parts': [{'text': fenced_text}],
                    },
                    'finishReason': 'STOP',
                }],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    assert response.json()['summary'] == result['summary']
    assert response.json()['findings'][0]['title'] == 'Trip hazard'

def test_analyze_handles_null_or_missing_limitations(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    result = {
        'summary': 'Response with null limitations',
        'findings': [],
        'limitations': None,
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{
                    'content': {
                        'role': 'model',
                        'parts': [{'text': json.dumps(result)}],
                    },
                    'finishReason': 'STOP',
                }],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    response = client.post('/api/analyze?mode=crack', files={'image': ('crack.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    assert response.json()['summary'] == result['summary']
    assert 'No precise crack dimensions can be inferred without a scale reference.' in response.json()['limitations']

def test_analyze_uses_flash_lite_url_when_stale_gemini_38_is_in_environment(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-3.8-flash')
    calls = []
    result = {
        'summary': 'Observation with stale env var',
        'findings': [],
        'limitations': [],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{
                    'content': {
                        'role': 'model',
                        'parts': [{'text': json.dumps(result)}],
                    },
                    'finishReason': 'STOP',
                }],
            }).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, 'urlopen', fake_urlopen)
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].endswith('/models/gemini-3.5-flash-lite:generateContent')

def test_analyze_crack_analysis_success(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    crack_result = {
        'summary': 'Visible diagonal shear cracking near beam-column connection.',
        'findings': [{
            'title': 'Diagonal shear crack',
            'detail': 'Step-like fracture observed near junction.',
            'severity': 'high',
            'confidence': 0.88,
            'action': 'Install temporary structural propping and consult licensed engineer.',
        }],
        'limitations': ['Visual observation only; internal reinforcement state not determined.'],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{
                    'content': {
                        'role': 'model',
                        'parts': [{'text': json.dumps(crack_result)}],
                    },
                    'finishReason': 'STOP',
                }],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    response = client.post('/api/analyze?mode=crack', files={'image': ('crack.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    data = response.json()
    assert data['mode'] == 'crack'
    assert data['provider'] == 'Gemini'
    assert data['summary'] == crack_result['summary']
    assert len(data['findings']) == 1
    assert data['findings'][0]['severity'] == 'high'
    assert 'No precise crack dimensions can be inferred without a scale reference.' in data['limitations']

@pytest.mark.parametrize('rest_envelope, expected_message', [
    ({'candidates': []}, 'missing provider candidates'),
    ({'candidates': [{'finishReason': 'STOP'}]}, 'missing provider candidate content'),
    ({'candidates': [{'content': {'parts': []}}]}, 'missing provider candidate parts'),
    ({'candidates': [{'content': {'parts': [{'text': 'thinking', 'thought': True}]}}]}, 'missing provider answer text'),
])
def test_analyze_missing_candidates_or_content_or_parts_or_text(monkeypatch, caplog, rest_envelope, expected_message):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(rest_envelope).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert expected_message in caplog.text
    assert 'stage=gemini_extraction' in caplog.text

def test_analyze_handles_blocked_model_response(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    class FakeBlockedPromptResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({'promptFeedback': {'blockReason': 'SAFETY'}}).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeBlockedPromptResponse())
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert 'provider blocked content' in caplog.text

    class FakeBlockedCandidateResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{'finishReason': 'SAFETY', 'content': {'parts': [{'text': ''}]}}],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeBlockedCandidateResponse())
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 502
    assert 'provider blocked content' in caplog.text

def test_analyze_handles_malformed_json_and_non_object(monkeypatch, caplog):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    class FakeResponse:
        def __init__(self, raw_text): self.raw_text = raw_text
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{'content': {'parts': [{'text': self.raw_text}]}}],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse('Not valid JSON at all'))
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})
    assert response.status_code == 502
    assert 'exception_type=JSONDecodeError' in caplog.text

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse('["not", "a", "dict"]'))
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})
    assert response.status_code == 502
    assert 'provider response is not a JSON object' in caplog.text

def test_analyze_nested_validation_error_diagnostics(monkeypatch, caplog):
    secret = 'secret-key-12345'
    image_bytes = b'confidential-site-photo'
    monkeypatch.setattr(main, 'gemini_api_key', lambda: secret)
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')
    invalid_data = {
        'summary': 'Hazard check',
        'findings': [
            {
                'title': 'Excavation edge',
                'detail': 'Unguarded pit',
                'severity': 'extreme',  # Invalid enum value
                'confidence': 1.5,       # Exceeds maximum 1.0
                # 'action' missing
            },
        ],
        'limitations': [],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{'content': {'parts': [{'text': json.dumps(invalid_data)}]}}],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', image_bytes, 'image/png')})

    assert response.status_code == 502
    assert response.json()['detail'] == 'AI provider error or invalid response'
    assert 'exception_type=ValidationError' in caplog.text
    assert 'stage=response_model' in caplog.text
    assert 'model=gemini-3.5-flash-lite' in caplog.text
    assert 'findings.0.severity:literal_error' in caplog.text
    assert 'findings.0.confidence:less_than_equal' in caplog.text
    assert 'findings.0.action:missing' in caplog.text
    assert secret not in caplog.text
    assert image_bytes.decode() not in caplog.text

def test_log_provider_exception_non_standard_validation_error(caplog):
    class CustomValidationError(Exception):
        def errors(self, include_input=False, include_context=False):
            return [{'loc': ('findings', 0, 'severity'), 'type': 'literal_error'}]

    exc = CustomValidationError('synthetic validation failure')
    with caplog.at_level(logging.WARNING, logger='uvicorn.error'):
        main._log_provider_exception(exc, 'test-key', b'', stage='response_model', model='gemini-3.5-flash-lite')

    assert 'exception_type=CustomValidationError' in caplog.text
    assert 'stage=response_model' in caplog.text
    assert 'model=gemini-3.5-flash-lite' in caplog.text
    assert 'response schema validation failed (findings.0.severity:literal_error)' in caplog.text

def test_safety_score_calculation_and_bounds():
    from main import Finding, PPEItem, compute_safety_score

    assert compute_safety_score([], []) is None

    # Perfect site
    ppe_all_observed = [
        PPEItem(item="Hard hats", status="observed", explanation="All workers wearing helmets"),
        PPEItem(item="High-visibility vests", status="observed", explanation="All workers wearing vests"),
    ]
    assert compute_safety_score([], ppe_all_observed) == 100

    # Low severity finding
    f_low = [Finding(title="Dust", detail="Minor dust", severity="low", confidence=0.8, action="Water spray")]
    score_low = compute_safety_score(f_low, ppe_all_observed)
    assert 94 <= score_low <= 98

    # High severity finding capped at 65
    f_high = [Finding(title="Unguarded edge", detail="No barrier", severity="high", confidence=0.9, action="Barricade")]
    score_high = compute_safety_score(f_high, ppe_all_observed)
    assert score_high <= 65

    # Missing PPE drops score further
    ppe_missing = [
        PPEItem(item="Hard hats", status="not_observed", explanation="Workers missing helmets"),
        PPEItem(item="High-visibility vests", status="not_observed", explanation="No vests visible"),
    ]
    score_high_missing_ppe = compute_safety_score(f_high, ppe_missing)
    assert score_high_missing_ppe <= 45

    # Multiple severe hazards capped at 40
    f_many_high = [
        Finding(title=f"Hazard {i}", detail="Severe risk", severity="high", confidence=0.95, action="Fix")
        for i in range(4)
    ]
    score_severe = compute_safety_score(f_many_high, ppe_missing)
    assert 5 <= score_severe <= 40

def test_analyze_safety_audit_score_ppe_and_severity_sorting(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    raw_safety_result = {
        'summary': 'Site safety observation shows active scaffolding and ground hazards.',
        'findings': [
            {
                'title': 'Loose tools on walkway',
                'detail': 'Hand tools lying in traffic area',
                'severity': 'low',
                'confidence': 0.75,
                'action': 'Clean up walkway',
            },
            {
                'title': 'Unprotected slab perimeter',
                'detail': 'Second level slab lacks perimeter edge protection',
                'severity': 'high',
                'confidence': 0.92,
                'action': 'Install rigid guardrails immediately',
                'location': 'Level 2 perimeter',
                'risk_explanation': 'Fall from height risk',
            },
            {
                'title': 'Improper ladder angle',
                'detail': 'Access ladder not secured at top',
                'severity': 'medium',
                'confidence': 0.85,
                'action': 'Secure ladder top and verify 4:1 slope',
            },
        ],
        'ppe_evaluations': [
            {'item': 'Hard hats', 'status': 'observed', 'explanation': 'Visible workers wearing hard hats.'},
            {'item': 'Fall-arrest/restraint equipment', 'status': 'not_observed', 'explanation': 'Work at height observed without harness or edge protection.'},
        ],
        'limitations': ['Visual observations only.'],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{'content': {'parts': [{'text': json.dumps(raw_safety_result)}]}}],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    response = client.post('/api/analyze?mode=safety', files={'image': ('site.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    data = response.json()
    assert data['mode'] == 'safety'
    assert isinstance(data['safety_score'], int)
    assert 0 <= data['safety_score'] <= 65  # capped due to high hazard
    # Verify severity ordering: high -> medium -> low
    severities = [f['severity'] for f in data['findings']]
    assert severities == ['high', 'medium', 'low']
    # Verify all 4 required PPE categories are present
    ppe_items = {item['item']: item for item in data['ppe_checklist']}
    assert 'Hard hats' in ppe_items
    assert 'High-visibility vests' in ppe_items
    assert 'Safety footwear' in ppe_items
    assert 'Fall-arrest/restraint equipment' in ppe_items
    assert ppe_items['Hard hats']['status'] == 'observed'
    assert ppe_items['Fall-arrest/restraint equipment']['status'] == 'not_observed'
    assert ppe_items['Safety footwear']['status'] == 'not_assessable'
    # Prioritized actions
    assert any('Immediate:' in act for act in data['prioritized_actions'])
    assert any('Priority:' in act for act in data['prioritized_actions'])
    assert any('Routine:' in act for act in data['prioritized_actions'])

def test_crack_mode_keeps_safety_fields_null(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    crack_data = {
        'summary': 'Linear hairline crack along plaster joint.',
        'findings': [{
            'title': 'Plaster joint crack',
            'detail': 'Non-structural separation',
            'severity': 'low',
            'confidence': 0.8,
            'action': 'Monitor and fill during cosmetic refurbishment',
        }],
        'limitations': [],
    }

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({
                'candidates': [{'content': {'parts': [{'text': json.dumps(crack_data)}]}}],
            }).encode()

    monkeypatch.setattr(main.urllib.request, 'urlopen', lambda *_args, **_kwargs: FakeResponse())
    response = client.post('/api/analyze?mode=crack', files={'image': ('crack.png', b'test-image', 'image/png')})

    assert response.status_code == 200
    data = response.json()
    assert data['mode'] == 'crack'
    assert data['safety_score'] is None
    assert data['ppe_checklist'] is None
    assert data['prioritized_actions'] is None

def test_assistant_with_full_site_context(monkeypatch):
    monkeypatch.setattr(main, 'gemini_api_key', lambda: 'test-only-key')
    monkeypatch.setattr(main, 'gemini_model', lambda: 'gemini-3.5-flash-lite')

    captured_prompt = []

    def fake_gemini(prompt, **kwargs):
        captured_prompt.append(prompt)
        return json.dumps({'answer': 'Slab casting can proceed provided rain cover and lighting are ready.'})

    monkeypatch.setattr(main, 'gemini', fake_gemini)

    payload = {
        'message': 'Can we proceed with slab casting tonight under rain?',
        'kind': 'chat',
        'site_context': {
            'activity': 'Slab casting',
            'labour_count': 15,
            'site_condition': 'Wet/Slippery',
            'location': 'Pune',
            'weather_summary': 'Light rain, 23C, 80% precipitation',
            'work_timing': 'Night shift (19:00 - 03:00)',
            'notes': 'Concrete pump on site with backup generator',
        },
    }

    response = client.post('/api/assistant', json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data['provider'] == 'Gemini'
    assert 'Slab casting' in data['context_summary']
    assert '15 workers' in data['context_summary']
    assert 'Wet/Slippery' in data['context_summary']
    assert 'Pune' in data['context_summary']
    assert len(captured_prompt) == 1
    assert 'Planned Activity: Slab casting' in captured_prompt[0]
    assert 'Workforce Count: 15 workers on site' in captured_prompt[0]
    assert 'Ground & Site Conditions: Wet/Slippery' in captured_prompt[0]

def test_assistant_offline_fallback_with_context(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setattr(main, 'gemini_api_key', lambda: None)

    payload = {
        'message': 'What precautions are needed?',
        'kind': 'chat',
        'site_context': {
            'activity': 'Deep excavation',
            'labour_count': 6,
            'site_condition': 'Muddy ground with high water table',
            'location': 'Bengaluru',
            'weather_summary': 'Heavy rain warning',
        },
    }

    response = client.post('/api/assistant', json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data['provider'] == 'Offline guidance'
    assert 'Deep excavation' in data['answer']
    assert 'Muddy ground' in data['answer']
    assert len(data['guidance']) >= 2

def test_assistant_daily_report_mode_with_context(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setattr(main, 'gemini_api_key', lambda: None)

    payload = {
        'message': 'Poured 45 cubic meters of concrete, no safety incidents.',
        'kind': 'daily_report',
        'site_context': {
            'activity': 'Column and beam casting',
            'labour_count': 18,
            'site_condition': 'Normal',
            'location': 'Mumbai',
        },
    }

    response = client.post('/api/assistant', json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data['provider'] == 'Guided template'
    assert 'Daily Site Report Structure' in data['answer']
    assert 'Column and beam casting' in data['answer']
    assert 'Mumbai' in data['answer']


import ssl

def test_cors_preflight_localhost_5174():
    response = client.options(
        "/api/assistant",
        headers={
            "Origin": "http://localhost:5174",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5174"
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert "POST" in response.headers.get("access-control-allow-methods", "")

def test_cors_preflight_localhost_5173():
    response = client.options(
        "/api/assistant",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

def test_cors_production_origin_allowed():
    response = client.options(
        "/api/assistant",
        headers={
            "Origin": "https://construct-iq-eight.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://construct-iq-eight.vercel.app"

    get_resp = client.get("/api/health", headers={"Origin": "https://construct-iq-eight.vercel.app"})
    assert get_resp.status_code == 200
    assert get_resp.headers.get("access-control-allow-origin") == "https://construct-iq-eight.vercel.app"

def test_cors_unapproved_origin_rejected():
    response = client.options(
        "/api/assistant",
        headers={
            "Origin": "http://unapproved-origin.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers

    get_resp = client.get("/api/health", headers={"Origin": "http://unapproved-origin.example.com"})
    assert get_resp.status_code == 200
    assert "access-control-allow-origin" not in get_resp.headers

def test_assistant_accepts_frontend_request_schema_with_site_context(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "gemini_api_key", lambda: None)

    payload = {
        "message": "Is concrete pouring advisable today?",
        "kind": "chat",
        "site_context": {
            "activity": "Concrete slab pour",
            "labour_count": 14,
            "site_condition": "Muddy ground after overnight drizzle",
            "location": "Pune, Maharashtra",
            "weather_summary": "Pune: Light drizzle, 26 °C, precip 1.2 mm, wind 9 km/h (live weather)",
            "work_timing": "Morning shift (08:00 - 16:00)",
            "notes": "Ensure curing blankets and sump pumps are on standby",
        },
    }
    response = client.post("/api/assistant", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "Offline guidance"
    assert "Concrete slab pour" in data["answer"]
    assert "Muddy ground after overnight drizzle" in data["answer"]
    assert data["context_summary"] is not None
    assert "Workforce Count: 14 workers on site" in data["context_summary"]
    assert "Weather Context: Pune: Light drizzle" in data["context_summary"]

def test_weather_ssl_verification_maintained_and_failures_handled_gracefully(monkeypatch, caplog):
    ssl_ctx = main._weather_ssl_context()
    assert isinstance(ssl_ctx, ssl.SSLContext)
    assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED
    assert ssl_ctx.check_hostname is True

    def ssl_failure(*_args, **_kwargs):
        raise main.URLError(ssl.SSLError("certificate verify failed: certificate has expired"))

    monkeypatch.setattr(main.urllib.request, "urlopen", ssl_failure)
    with caplog.at_level(logging.WARNING):
        response = client.get("/api/weather", params={"location": "Pune"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Location lookup is unavailable. Try again shortly."
    assert "stage=geocoding kind=network_error" in caplog.text

def test_assistant_handles_manual_weather_when_live_weather_fails(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(main, "gemini_api_key", lambda: None)

    manual_weather = "Manual entry: 32 °C Sunny, dry compact ground, light breeze"
    payload = {
        "message": "Review roofing and scaffolding safety requirements.",
        "kind": "chat",
        "site_context": {
            "activity": "Roof truss installation",
            "labour_count": 8,
            "site_condition": "Dry / firm footing",
            "location": "Pune site",
            "weather_summary": manual_weather,
            "work_timing": "Full day shift",
            "notes": "High wind limit is 25 km/h",
        },
    }
    response = client.post("/api/assistant", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "Offline guidance"
    assert "Roof truss installation" in data["answer"]
    assert manual_weather in data["context_summary"]
