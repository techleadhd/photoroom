"""Optional Lua 5.1 codec and SDK-adapter tests: pip install lupa.

These are mocked SDK tests, not substitutes for a real Classic smoke test.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

PLUGIN=Path(__file__).resolve().parents[1]/'PhotoRoom.lrplugin'


@unittest.skipIf(LuaRuntime is None,'Optional lupa test dependency not installed')
class LuaTests(unittest.TestCase):
    def test_codec_roundtrip_and_unsafe_input(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        codec=lua.execute((PLUGIN/'Json.lua').read_text())
        data={'path':'/Photos/café/😀/a "quote"\n.CR3','settings':{'Exposure2012':1.25,'HasCrop':False},'curve':[0,0,255,255]}
        for ascii_only in [False,True]:
            encoded=codec.encode(codec.decode(json.dumps(data,ensure_ascii=ascii_only)))
            self.assertEqual(json.loads(encoded),data)
        for text in ['os.execute("rm")','{"test": [1,}','{"x":1} trailing']:
            with self.assertRaises(Exception): codec.decode(text)

    def test_listener_menu_reports_status_and_does_not_start_duplicates(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
            _PLUGIN={path='/plugin'}
            local modules={
                LrDialogs={message=function(title,text,kind) MESSAGE=text end},
                LrPathUtils={child=function(a,b)return a..'/'..b end},
            }
            function import(name)return assert(modules[name])end
            function dofile(path)
                assert(path=='/plugin/Bridge.lua')
                STARTS=(STARTS or 0)+1
                photoRoomListener={status='starting',stopping=false}
            end
        """)
        menu=lua.execute((PLUGIN/'ListenerMenu.lua').read_text())
        g=lua.globals()
        menu.status();self.assertTrue(g.MESSAGE.startswith('Stopped.'))
        menu.stop();self.assertIsNone(g.STARTS)
        menu.start();self.assertEqual(g.STARTS,1)
        self.assertTrue(g.MESSAGE.startswith('Starting.'))
        for state,prefix in [('ready','Ready.'),('matching','Matching.'),('cleaning up','Cleaning up')]:
            g.photoRoomListener.status=state
            menu.start()
            self.assertTrue(g.MESSAGE.startswith(prefix))
            self.assertEqual(g.STARTS,1)
        menu.stop();self.assertTrue(g.photoRoomListener.stopping)
        self.assertTrue(g.MESSAGE.startswith('Stopping.'))
        menu.start();self.assertEqual(g.STARTS,2)

    def test_expired_heartbeat_restores_unfinished_photo(self):
        self.bridge_case(virtual=False,rotate=1,crash=True)

    def test_expired_heartbeat_keeps_completed_photo(self):
        self.bridge_case(virtual=False,commit=True,crash=True)

    def test_disabling_active_plugin_restores_photo(self):
        self.bridge_case(virtual=False,rotate=1,disable_active=True)

    def test_stale_session_is_not_replayed_on_startup(self):
        self.bridge_case(stale=True)

    def test_stop_menu_exits_bridge_and_removes_lock(self):
        self.bridge_case(menu_stop=True)

    def test_bridge_virtual_copy_render(self):
        self.bridge_case()

    def test_bridge_accepts_two_runs_without_relaunch_or_folder_prompt(self):
        self.bridge_case(virtual=False,commit=True,repeat_run=True)

    def test_plugin_can_start_before_python(self):
        self.bridge_case(plugin_first=True)

    def test_sdk_error_identifies_operation(self):
        self.bridge_case(fail_export=True)

    def test_direct_rotation_restored_on_stop(self):
        self.bridge_case(virtual=False,rotate=1)

    def test_direct_rotation_restored_on_abort(self):
        self.bridge_case(virtual=False,rotate=-1,abort=True)

    def test_commit_can_be_rolled_back_if_completion_marker_fails(self):
        self.bridge_case(virtual=False,rotate=1,commit=True,abort=True)

    def test_committed_rotation_is_retained(self):
        self.bridge_case(virtual=False,rotate=2,commit=True)

    def test_virtual_copy_rotation_leaves_master_unchanged(self):
        self.bridge_case(rotate=1)

    def test_export_failure_after_rotation_restores_master(self):
        self.bridge_case(virtual=False,rotate=1,fail_export=True)

    def test_develop_patch_omits_opaque_fields_and_restores_controls(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.globals().DEVELOP_PATH=str(PLUGIN/'Develop.lua')
        lua.execute('''
            local develop=dofile(DEVELOP_PATH)
            local base={Exposure2012=0,WhiteBalance='As Shot',Look=false,OpaqueState=true}
            local previous=develop.controls(base)
            local patch,target=develop.patch({Exposure2012=1,WhiteBalance='As Shot',Look=false},previous,previous)
            assert(patch.Exposure2012==1 and patch.Look==nil and patch.OpaqueState==nil)
            local reset=develop.patch(base,target,previous)
            assert(reset.Exposure2012==0)
            local unchanged=develop.patch(base,previous,previous)
            assert(next(unchanged)==nil)
            local hsl={Exposure2012=0,WhiteBalance='As Shot',HueAdjustmentRed=20}
            local hslReset=develop.patch(base,hsl,previous)
            assert(hslReset.HueAdjustmentRed==nil)
            local forbidden={'Texture','Clarity2012','Dehaze','Vibrance','Saturation','Sharpness',
                'HueAdjustmentRed','SaturationAdjustmentBlue','LuminanceAdjustmentGreen'}
            local desired={Exposure2012=1}
            for _,name in ipairs(forbidden) do desired[name]=42 end
            local safe=develop.patch(desired,previous,previous)
            assert(safe.Exposure2012==1)
            for _,name in ipairs(forbidden) do assert(safe[name]==nil,name) end
        ''')

    def bridge_case(self,fail_export=False,virtual=True,rotate=0,commit=False,abort=False,repeat_run=False,plugin_first=False,menu_stop=False,crash=False,stale=False,disable_active=False):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'match-session';root.mkdir()
            (root/'bridge-running').write_text('stale lock from previous Lightroom process')
            jobs=[{'id':'hello','action':'hello'},
                  {'id':'one','action':'begin','source':'/orig/file.CR3'},
                  {'id':'two','action':'render','source':'/orig/file.CR3','settings':{'Exposure2012':1.25}},
                  {'id':'four','action':'stop'}]
            if rotate: jobs.insert(2,{'id':'orient','action':'orient','source':'/orig/file.CR3','settings':{'quarter_turns_ccw':rotate}})
            if commit: jobs.insert(-1,{'id':'commit','action':'commit','source':'/orig/file.CR3'})
            if abort: jobs.insert(-1,{'id':'abort','action':'abort','source':'/orig/file.CR3'})
            for job in jobs: job['run_id']='test'
            if repeat_run:
                jobs += [{'id':'second-hello','action':'hello','run_id':'second'},
                         {'id':'second-begin','action':'begin','source':'/orig/file.CR3','run_id':'second'},
                         {'id':'second-render','action':'render','source':'/orig/file.CR3','run_id':'second','settings':{'Exposure2012':2.5}},
                         {'id':'second-commit','action':'commit','source':'/orig/file.CR3','run_id':'second'},
                         {'id':'second-stop','action':'stop','run_id':'second'}]
            if crash or disable_active: jobs = [j for j in jobs if j['action'] != 'stop']
            def publish(job):
                (root/'session.json').write_text(json.dumps({'protocol':3,'run_id':job['run_id'],'virtual_copies':virtual}))
                (root/'request.json').write_text(json.dumps(job))
                (root/'python-heartbeat.json').write_text(json.dumps({'run_id':job['run_id'],'time':time.time()-60 if stale else time.time()}))
            if not plugin_first: publish(jobs[0])
            current=[-1 if plugin_first else 0]
            expired=[False]
            def next_job():
                # A second init/enable/menu call must reuse the existing task.
                lua.execute((PLUGIN/'Bridge.lua').read_text())
                if stale:
                    self.assertIsNone(g.PROGRESS)
                    self.assertIsNone(g.applied)
                    g.photoRoomListener.stopping=True
                elif current[0]+1 < len(jobs):
                    if current[0]>=0 and jobs[current[0]]['action']=='stop':
                        self.assertFalse((root/'bridge-running').exists())
                        self.assertEqual(g.OPEN_PROGRESS,0)
                    current[0]+=1; publish(jobs[current[0]])
                elif crash and not expired[0]:
                    (root/'python-heartbeat.json').write_text(json.dumps({'run_id':'test','time':time.time()-60}))
                    expired[0]=True
                else:
                    if crash:
                        self.assertFalse((root/'bridge-running').exists())
                        self.assertEqual(g.OPEN_PROGRESS,0)
                        self.assertEqual(g.MASTER.settings.Exposure2012,1.25 if commit else 0)
                    lua.execute((PLUGIN/('StopListener.lua' if menu_stop else 'StopBridge.lua')).read_text())
            lua=LuaRuntime(unpack_returned_tuples=True)
            g=lua.globals();g.ROOT=str(root);g.ROOT_PARENT=str(Path(d));g.PLUGIN_PATH=str(PLUGIN);g.next_job=next_job
            g.FAIL_EXPORT=fail_export
            g.VIRTUAL=virtual
            g.mkdir=lambda p:Path(p).mkdir(parents=True,exist_ok=True)
            lua.execute(r'''
                _PLUGIN={path=PLUGIN_PATH}
                local copy={settings={Exposure2012=0,ProcessVersion='11.0',Look=false,OpaqueState=true,orientation='AB'}}
                function copy:getDevelopSettings() return self.settings end
                function copy:getRawMetadata(key) if key=='fileFormat' then return 'RAW' end end
                function copy:applyDevelopSettings(s)
                    assert(s.Look==nil and s.OpaqueState==nil,'Opaque fields must not be applied')
                    for k,v in pairs(s)do self.settings[k]=v end
                    applied=(applied or 0)+1
                end
                local orientations={AB='DA',DA='CD',CD='BC',BC='AB'}
                function copy:rotateLeft() self.settings.orientation=orientations[self.settings.orientation] end
                function copy:rotateRight() for _=1,3 do self:rotateLeft() end end
                local master={settings={Exposure2012=0,ProcessVersion='11.0',Look=false,OpaqueState=true,orientation='AB'}}
                master.getDevelopSettings=copy.getDevelopSettings
                master.applyDevelopSettings=copy.applyDevelopSettings
                master.rotateLeft=copy.rotateLeft;master.rotateRight=copy.rotateRight
                function master:getRawMetadata(key) if key=='fileFormat' then return 'RAW' else return false end end
                MASTER=master;COPY=copy
                local catalog={}
                function catalog:getTargetPhoto() return master end
                function catalog:getTargetPhotos() return {master} end
                function catalog:setSelectedPhotos(photo,photos) selected=photo end
                function catalog:findPhotoByPath(path) assert(path=='/orig/file.CR3'); return master end
                function catalog:createVirtualCopies(name)
                    assert(selected==master); copies=(copies or 0)+1; return {copy}
                end
                function catalog:withWriteAccessDo(name,fn,options) fn() end
                local function session(args)
                    if FAIL_EXPORT then error('?:0: attempt to index a boolean value') end
                    assert(args.photosToExport[1]==(VIRTUAL and copy or master),'Must export the active photo')
                    local es=args.exportSettings
                    assert(es.LR_embeddedMetadataOption=='all')
                    assert(es.LR_export_colorSpace=='ProPhotoRGB')
                    assert(es.LR_tiff_bitDepth==16)
                    assert(es.LR_format=='TIFF','Only pixel renders may be exported')
                    local path=es.LR_export_destinationPathPrefix..'/file.tif'
                    local f=assert(io.open(path,'wb'));f:write('mock');f:close()
                    local rendition={waitForRender=function() return true,path end}
                    return {renditions=function() local done=false;return function()
                        if not done then done=true;return 1,rendition end
                    end end}
                end
                local tasks={startAsyncTask=function(fn)fn()end,pcall=pcall,sleep=function()next_job()end}
                local contextModule={callWithContext=function(name,fn)
                    local handlers={};fn({addCleanupHandler=function(self,f)handlers[#handlers+1]=f end})
                    for _,f in ipairs(handlers)do f()end
                end}
                local modules={
                    LrApplication={activeCatalog=function()return catalog end},
                    LrDialogs={runOpenPanel=function()error('Must not prompt for a folder')end,message=function(a,b,kind)DIALOGS=(DIALOGS or 0)+1;if kind~='info' then error(b) end end},
                    LrTasks=tasks,
                    LrPathUtils={child=function(a,b)return a..'/'..b end,parent=function(p)assert(p==PLUGIN_PATH);return ROOT_PARENT end},
                    LrFileUtils={createAllDirectories=function(p)mkdir(p)end,
                        exists=function(p)local f=io.open(p,'rb');if f then f:close();return true end;return false end,
                        delete=os.remove,move=os.rename},
                    LrExportSession=session,LrFunctionContext=contextModule,
                    LrProgressScope=function()
                        OPEN_PROGRESS=(OPEN_PROGRESS or 0)+1
                        PROGRESS={isCanceled=function()return CANCELLED==true end,setCaption=function()end,
                            done=function()OPEN_PROGRESS=OPEN_PROGRESS-1 end}
                        return PROGRESS
                    end,
                }
                function import(name)assert(modules[name],name);return modules[name]end
            ''')
            # Lightroom omits os.remove; SDK mocks above retain their host functions.
            lua.execute('os.remove = nil')
            lua.execute((PLUGIN/'Bridge.lua').read_text())
            self.assertIsNone(g.photoRoomListener)
            self.assertEqual(g.DIALOGS,1 if menu_stop else None)
            if stale:
                self.assertFalse((root/'bridge-running').exists())
                self.assertFalse(list(root.glob('response-*.json')))
                return
            self.assertEqual(g.OPEN_PROGRESS,0)
            self.assertEqual(g.copies,1 if virtual else None)
            self.assertEqual(g.applied,2 if repeat_run else (1 if virtual or (commit and not abort) else 2))
            desired=['AB','DA','CD','BC'][rotate % 4]
            self.assertEqual(g.MASTER.settings.orientation,desired if commit and not abort and not virtual else 'AB')
            self.assertEqual(g.MASTER.settings.Exposure2012,2.5 if repeat_run else (1.25 if commit and not abort and not virtual else 0))
            if virtual:self.assertEqual(g.COPY.settings.orientation,desired)
            self.assertFalse((root/'bridge-running').exists())
            self.assertFalse((root/'stop-bridge').exists())
            if crash or disable_active:
                self.assertEqual((root/'cancelled').read_text(), 'test')
            else:
                self.assertFalse((root/'cancelled').exists())
            responses=[json.loads((root/f'response-{j["id"]}.json').read_text()) for j in jobs]
            hello=next(r for r in responses if r['id']=='hello')
            self.assertTrue(hello['ready'])
            self.assertEqual(hello['bridge_version'],'0.3.0')
            render_response=next(r for r in responses if r['id']=='two')
            if fail_export:
                self.assertEqual(render_response['stage'],'createExportSession')
                self.assertIn('[render/createExportSession]',render_response['error'])
                self.assertIn('attempt to index a boolean value',render_response['error'])
                self.assertIn('failed:createExportSession',(root/'bridge.log').read_text())
            else:
                self.assertTrue(all('error' not in r for r in responses))
                self.assertEqual(render_response['settings']['Exposure2012'],1.25)
                self.assertTrue(render_response['path'].endswith('.tif'))
                self.assertEqual(render_response['run_id'],'test')


if __name__=='__main__':unittest.main()
