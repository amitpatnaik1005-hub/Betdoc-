"""Live Engine Room WebSocket tests (Starlette's synchronous TestClient)."""



import time

from datetime import date



import pytest

from fastapi import FastAPI

from fastapi.testclient import TestClient



from app.api.v1 import the_core as core_api

from app.api.v1.the_core import ConnectionManager

from app.models.the_core import BacktestJobModel, EngineTaskStatus, TestBenchRunModel



LIVE = "/api/v1/engine/live"





@pytest.fixture

def manager():

    return ConnectionManager()





@pytest.fixture

def app(manager):

    application = FastAPI()

    application.include_router(core_api.router, prefix="/api/v1/engine")

    application.dependency_overrides[core_api.get_ws_manager] = lambda: manager

    return application





def test_handshake_sends_welcome(app, manager):

    with TestClient(app) as client, client.websocket_connect(LIVE) as ws:

        hello = ws.receive_json()

        assert hello["event"] == "engine.connected"

        assert hello["payload"]["master_bot"] == "PRATAP"

        assert hello["payload"]["math_engine"] == "PANINI"

        assert manager.active_connections == 1





def test_ping_pong(app):

    with TestClient(app) as client, client.websocket_connect(LIVE) as ws:

        ws.receive_json()

        ws.send_text("ping")

        pong = ws.receive_json()

        assert pong["event"] == "engine.pong"

        assert pong["payload"]["active_connections"] == 1





def test_broadcast_fans_out_to_every_client(app, manager):

    event = {"event": "test_bench.state", "source": "PRATAP", "payload": {"status": "RUNNING"}}

    with TestClient(app) as client:

        with client.websocket_connect(LIVE) as ws1, client.websocket_connect(LIVE) as ws2:

            ws1.receive_json()

            ws2.receive_json()

            assert manager.active_connections == 2

            client.portal.call(manager.broadcast, event)

            assert ws1.receive_json() == event

            assert ws2.receive_json() == event





def test_disconnect_prunes_connection(app, manager):

    with TestClient(app) as client:

        with client.websocket_connect(LIVE) as ws:

            ws.receive_json()

            assert manager.active_connections == 1

        for _ in range(50):

            client.portal.call(manager.broadcast, {"event": "engine.heartbeat", "payload": {}})

            if manager.active_connections == 0:

                break

            time.sleep(0.02)

        assert manager.active_connections == 0





def test_test_bench_worker_streams_conveyor_belt(app, manager, orch, sqlite_database, match_context):

    async def scenario():

        # The DB engine is created inside the TestClient portal loop on purpose.

        async with sqlite_database() as factory:

            async with factory() as session:

                smallcases = await orch.bootstrap_smallcases(session)

                target = next(s for s in smallcases if s.pipeline_config[0] == "PoissonModel")

                run = await orch.create_test_bench_run(session, target.id, match_context)

                run_id = run.id

            await orch.execute_test_bench(factory, run_id, manager)

            async with factory() as session:

                final = await session.get(TestBenchRunModel, run_id)

                return str(run_id), final.status



    with TestClient(app) as client, client.websocket_connect(LIVE) as ws:

        ws.receive_json()

        run_id, final_status = client.portal.call(scenario)

        assert final_status == EngineTaskStatus.COMPLETED



        events = [ws.receive_json() for _ in range(4)]

        assert [e["event"] for e in events] == [

            "test_bench.state",

            "test_bench.step",

            "test_bench.step",

            "test_bench.state",

        ]

        assert events[0]["payload"]["status"] == "RUNNING"

        assert events[0]["payload"]["run_id"] == run_id

        assert [e["payload"]["step"]["model"] for e in events[1:3]] == ["PoissonModel", "KellyStake"]

        assert events[1]["payload"]["step"]["mode"] == "NATIVE"

        assert events[3]["payload"]["status"] == "COMPLETED"

        assert events[3]["payload"]["predicted_outcome"]["pick"] == "home"





def test_backtest_worker_streams_state(app, manager, orch, sqlite_database):

    async def scenario():

        async with sqlite_database() as factory:

            async with factory() as session:

                smallcases = await orch.bootstrap_smallcases(session)

                target = next(s for s in smallcases if s.pipeline_config[0] == "PoissonModel")

                job = await orch.create_backtest_job(session, target.id, date(2026, 1, 1), date(2026, 1, 1))

                job_id = job.id

            await orch.execute_backtest_job(factory, job_id, manager)

            async with factory() as session:

                final = await session.get(BacktestJobModel, job_id)

                return str(job_id), final.status



    with TestClient(app) as client, client.websocket_connect(LIVE) as ws:

        ws.receive_json()

        job_id, final_status = client.portal.call(scenario)

        assert final_status == EngineTaskStatus.COMPLETED



        running = ws.receive_json()

        completed = ws.receive_json()

        assert running["event"] == completed["event"] == "backtest.state"

        assert running["payload"]["status"] == "RUNNING"

        assert running["payload"]["job_id"] == job_id

        assert completed["payload"]["status"] == "COMPLETED"

        assert completed["payload"]["total_matches_simulated"] == 250


