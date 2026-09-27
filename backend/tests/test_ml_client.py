"""Run separately from ml_service/tests (both services have an app package)."""
import asyncio
import sys
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'ml_service'),str(ROOT/'map_matching/src')]
from app.services.ml_client import MLServiceClient
from app.schemas.telemetry import MLFeaturesPayload,RawNDTPRecord

def test_preserves_fractional_timestamp():
    assert RawNDTPRecord(tr_id='1',timestamp=1000.125).timestamp==1000.125

def test_persistence_when_ml_unavailable():
    async def run():
        features=MLFeaturesPayload(tr_id='1',t_timestamp=1000,target_stop_id='2',
            planned_arrival_time=1700,current_delay_sec=73.25,current_speed_kmh=0,
            avg_speed_segment=0,dwell_time_sec=0)
        client=MLServiceClient()
        def fail(req):raise httpx.ConnectError('offline',request=req)
        client._client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
        result=await client.predict(features)
        assert result.prediction_delay_s==73.25
        assert result.model_version=='fallback:persistence'
        assert client.last_error and client.status=='fallback'
        await client._client.aclose()
    asyncio.run(run())
