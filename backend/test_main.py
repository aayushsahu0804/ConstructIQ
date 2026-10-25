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

