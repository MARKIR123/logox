"""Independent sleep selection: persistence, lifecycle and user command routing."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from logox.anamnesis.service import AnamesisService
from logox.app import Runtime
from logox.config.loader import load
from logox.config.schema import AnamesisConfig
from logox.config.state import LayeredStateStore, StateStore
from logox.paths import LogoxPaths
from logox.providers.registry import ProviderSpec
from logox.tui.render.commands import CommandRunner
from tests.unit.support import make_temp_dir, remove_temp_dir
from tests.tui.test_render_commands import FakeHost, FakeRuntime, _pick, command


class SleepModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = make_temp_dir('sleep-model-').resolve()
        self.addCleanup(remove_temp_dir,self.root)
        self.config = AnamesisConfig(provider='ollama', model='old')
        self.service = AnamesisService(config=self.config, home=self.root/'home', cwd=self.root,
                                      sessions=self.root/'sessions', runner_factory=AsyncMock())
        self.addAsyncCleanup(self.service.aclose)
        self.store = StateStore(self.root/'home'/'state.toml')
        self.spec = ProviderSpec(name='ollama', base_url='http://localhost:11434/v1',
                                 models=('old','new'), context_window=8192)
        self.runtime = Runtime(bus=None, kernel=Mock(), reducer=None, theme=None,
                               config=SimpleNamespace(anamnesis=self.config),
                               provider_name='cloud', model='conversation', cwd=self.root,
                               tools=[], state_store=self.store, anamnesis=self.service,
                               registry=SimpleNamespace(spec=lambda name:self.spec))
        self.runtime.list_models = Mock(return_value=['old','new'])

    async def test_switch_persists_independently_and_next_factory_reads_new_model(self):
        self.store.set_last_model(provider='cloud', model='conversation')
        observed=[]
        async def factory(collector):
            observed.append(self.config.model)
        self.service.runner_factory=factory
        self.assertEqual(await self.runtime.apply_anamnesis_model('new'),8192)
        await self.service.runner_factory(self.service.collector)
        self.assertEqual(observed,['new'])
        saved=self.store.read().last
        self.assertEqual((saved.provider,saved.model),('cloud','conversation'))
        self.assertEqual((saved.anamnesis_provider,saved.anamnesis_model),('ollama','new'))
        self.assertEqual(self.service.status().model,'new')
        self.assertEqual(self.runtime.model,'conversation')
        self.runtime.kernel.set_model.assert_not_called()

    async def test_running_task_refuses_without_writing_or_cancelling(self):
        task=asyncio.create_task(asyncio.sleep(60))
        self.service._task=task
        try:
            with self.assertRaisesRegex(ValueError,'运行'):
                await self.runtime.apply_anamnesis_model('new')
            self.assertFalse(task.cancelled())
            self.assertEqual(self.config.model,'old')
            self.assertFalse(self.store.path.exists())
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_preparation_lock_refuses_switch(self):
        async with self.service._start_lock:
            with self.assertRaisesRegex(ValueError,'准备'):
                await self.runtime.apply_anamnesis_model('new')
        self.assertFalse(self.store.path.exists())

    async def test_persistence_failure_leaves_selection_unchanged(self):
        self.store.set_last_anamnesis_model=Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):
            await self.runtime.apply_anamnesis_model('new')
        self.assertEqual(self.config.model,'old')
        self.assertEqual(self.service.status().model,'old')

    async def test_remote_endpoint_missing_model_or_small_window_refused(self):
        for spec,model in [
            (self.spec.model_copy(update={'base_url':'https://example.com/v1'}),'new'),
            (self.spec,'uninstalled'),
            (self.spec.model_copy(update={'context_window':2048}),'new'),
        ]:
            self.spec=spec
            with self.assertRaises(ValueError):
                await self.runtime.apply_anamnesis_model(model)
            self.assertEqual(self.config.model,'old')
            self.assertFalse(self.store.path.exists())

    async def test_refresh_rejects_remote_and_uses_only_local_sleep_provider(self):
        self.runtime.refresh_models = AsyncMock()
        await self.runtime.refresh_anamnesis_models()
        self.runtime.refresh_models.assert_awaited_once_with('ollama', timeout_s=3.0)
        self.runtime.refresh_models.reset_mock()
        self.spec = self.spec.model_copy(update={'base_url':'https://example.com/v1'})
        with self.assertRaises(ValueError):
            await self.runtime.refresh_anamnesis_models()
        self.runtime.refresh_models.assert_not_awaited()

    async def test_state_reload_and_environment_override(self):
        self.store.set_last_model(provider='ollama',model='old')
        self.store.set_last_anamnesis_model(provider='ollama',model='new')
        paths=LogoxPaths.at(self.root/'home')
        bundle=load(self.root,paths=paths,project_chain=[],state=self.store.read(),env={})
        self.assertEqual(bundle.config.anamnesis.model,'new')
        self.assertEqual(bundle.config.provider.model,'old')
        env={'LOGOX__ANAMNESIS__MODEL':'override'}
        bundle=load(self.root,paths=paths,project_chain=[],state=self.store.read(),env=env)
        self.assertEqual(bundle.config.anamnesis.model,'override')

    async def test_layered_state_preserves_project_foreground(self):
        global_store=StateStore(self.root/'global.toml')
        self.store.set_last_model(provider='project',model='project-model')
        global_store.set_last_model(provider='global',model='global-model')
        LayeredStateStore(self.store,global_store).set_last_anamnesis_model(provider='ollama',model='new')
        self.assertEqual(self.store.read().last.model,'project-model')
        self.assertEqual(global_store.read().last.model,'global-model')
        self.assertEqual(self.store.read().last.anamnesis_model,'new')
        self.assertEqual(global_store.read().last.anamnesis_model,'new')


class SleepCommandTests(unittest.IsolatedAsyncioTestCase):
    def make(self,answers=None):
        runtime=FakeRuntime()
        runtime.anamnesis=SimpleNamespace(config=SimpleNamespace(provider='ollama',model='llama3'))
        runtime.apply_anamnesis_model=AsyncMock(return_value=8192)
        runtime.refresh_anamnesis_models=AsyncMock(return_value=SimpleNamespace(ok=True,summary=lambda:'抓到 2 个模型'))
        host=FakeHost(runtime,answers)
        return CommandRunner(host),host,runtime

    async def test_direct_selection_keeps_foreground(self):
        runner,host,runtime=self.make()
        await runner.run(command('/anamnesis model llama3'))
        runtime.apply_anamnesis_model.assert_awaited_once_with('llama3')
        self.assertEqual(runtime.model,'deepseek-flash')
        self.assertEqual(runtime.kernel.models,[])
        self.assertIn('交流模型保持不变',host.text)

    async def test_picker_routes_to_sleep_selection(self):
        runner,host,runtime=self.make([lambda component:_pick(component,'llama3')])
        await runner.run(command('/anamnesis model'))
        runtime.apply_anamnesis_model.assert_awaited_once_with('llama3')

    async def test_cancel_changes_nothing(self):
        runner,host,runtime=self.make([None])
        await runner.run(command('/anamnesis model'))
        runtime.apply_anamnesis_model.assert_not_awaited()

    async def test_refresh_only_uses_sleep_provider(self):
        runner,host,runtime=self.make()
        await runner.run(command('/anamnesis model refresh'))
        runtime.refresh_anamnesis_models.assert_awaited_once_with()
        runtime.apply_anamnesis_model.assert_not_awaited()

    async def test_failure_is_visible(self):
        runner,host,runtime=self.make()
        runtime.apply_anamnesis_model.side_effect=ValueError('正在运行')
        await runner.run(command('/anamnesis model llama3'))
        self.assertIn('无法切换入梦模型',host.text)
        self.assertEqual(runtime.kernel.models,[])
