"""Explicit Temporal integration test (not part of the offline suite).

Runs against the Temporal time-skipping test server; point TEMPORAL_TEST_SERVER at the SDK's cached binary
(downloaded on first `WorkflowEnvironment.start_time_skipping()` call). Verifies that a completed TrialWorkflow id
is rejected for reuse (REJECT_DUPLICATE), the launch-once contract at the workflow layer.
"""
import asyncio,os,uuid
from temporalio import activity
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from swesim.workflows.workflows import TrialWorkflow
@activity.defn(name='launch_trial_activity')
async def launch(p):return {'trial_id':p['trial_id']}
@activity.defn(name='await_trial_activity')
async def collect(p):return {'trial_id':p['trial_id'],'status':'completed'}
def test_temporal_rejects_duplicate():
    async def run():
        async with await WorkflowEnvironment.start_time_skipping(test_server_existing_path=os.environ['TEMPORAL_TEST_SERVER']) as env:
            async with Worker(env.client,task_queue='swesim',workflows=[TrialWorkflow],activities=[launch,collect]):
                tid=uuid.uuid4().hex
                await env.client.execute_workflow(TrialWorkflow.run,{'trial_id':tid},id=tid,task_queue='swesim')
                try:
                    await env.client.start_workflow(TrialWorkflow.run,{'trial_id':tid},id=tid,task_queue='swesim',id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
                except WorkflowAlreadyStartedError:return
                raise AssertionError('duplicate completed workflow was accepted')
    asyncio.run(run())
