"""Manual web-session state, scoped to a single bot and its current API."""
from collections import deque
import threading
import time
import uuid
from logic.events import emitter

class ManualWeb:
    def __init__(self, manager):
        self.manager=manager
        self.lock=threading.RLock()
        self.sessions={}
        self.listeners=[]
        for name in ('onShowChat','onShowDialog','onSpawnSelect','onNpcDialog','onReceiveJSON','disconnect'):
            def handler(source,*args,_name=name): self.receive(_name,source,args)
            emitter.subscribe(name,handler,sync=True)
            self.listeners.append((name,handler))
    def close(self):
        for name,handler in self.listeners: emitter.off(name,handler)
        self.listeners=[]
    def receive(self,name,source,args):
        bot=self.manager._find_bot_by_session_or_api(source)
        if not bot:return
        with self.lock:
            s=self.sessions.get(bot.bot_id)
            if not s or not getattr(bot,'manual_web_control',False) or source is not getattr(bot,'api',None):return
            if name=='disconnect':
                s['window']=None;s['chat'].append({'type':'system','text':'Соединение прервано'});return
            if name=='onShowChat':
                s['chat'].append({'type':'chat','text':str(args[0])[:4000]});return
            if name=='onReceiveJSON' and len(args)>=2 and isinstance(args[1],dict):
                iid,data=args[0],args[1]
                if iid in (26,31):
                    if data.get('c')==1:
                        if s['window'] and s['window'].get('interface_id')==iid:s['window']=None
                    elif data.get('o')==1:
                        s['window']={'type':'unsupported','token':uuid.uuid4().hex,'interface_id':iid,
                                     'title':'Капча' if iid==26 else 'Мини-игра',
                                     'text':'Этот интерфейс пока не поддерживается ручной панелью.'}
                elif iid==63 and data.get('c')==1 and s['window'] and s['window'].get('type')=='npc':
                    s['window']=None
                return
            token=uuid.uuid4().hex
            if name=='onShowDialog' and len(args)>=6:
                did,style,title,text,b1,b2=args[:6]
                win={'type':'dialog','token':token,'id':int(did),'style':int(style),'title':str(title)[:500],
                     'text':str(text)[:12000],'button1':str(b1)[:120],'button2':str(b2)[:120]}
            elif name=='onSpawnSelect' and args and isinstance(args[0],dict):
                options=args[0].get('m')
                if not isinstance(options,list) or not all(isinstance(x,int) and not isinstance(x,bool) for x in options):return
                win={'type':'spawn','token':token,'options':options[:100]}
            elif name=='onNpcDialog' and len(args)>=4:
                n,t,model,buttons=args[:4]
                if not isinstance(buttons,list):buttons=[]
                safe=[]
                for b in buttons[:50]:
                    if isinstance(b,dict) and isinstance(b.get('key'),int):
                        safe.append({'key':b['key'],'text':str(b.get('text') or b.get('name') or '')[:120]})
                win={'type':'npc','token':token,'title':str(n)[:500],'text':str(t)[:12000],'buttons':safe}
            else:return
            s['window']=win
    def begin(self,bot):
        if getattr(bot,'lua_runtime',None) and bot.lua_runtime.running:raise ValueError('сначала остановите Lua-сценарий')
        if getattr(getattr(bot,'session',None),'connected',False) or (getattr(bot,'thread',None) and bot.thread.is_alive()):
            raise ValueError('для ручного входа сначала остановите аккаунт')
        if getattr(bot,'active_task',None):
            bot.active_task.stop();bot.active_task=None
        with self.lock:self.sessions[bot.bot_id]={'chat':deque(maxlen=200),'window':None}
        bot.manual_web_control=True;bot.lua_spawn_control=True;bot.lua_ui_control=True
        try:
            self.manager.refresh_bridge_proxy(bot)
            if not bot.start():raise ValueError('аккаунт не запустился; проверьте его лог')
        except Exception:
            bot.manual_web_control=False;bot.lua_spawn_control=False;bot.lua_ui_control=False
            with self.lock:self.sessions.pop(bot.bot_id,None)
            raise
    def stop(self,bot):
        if not getattr(bot,'manual_web_control',False):raise ValueError('ручной режим для этого аккаунта не запущен')
        bot.stop()
        bot.manual_web_control=False;bot.lua_spawn_control=False;bot.lua_ui_control=False
        with self.lock:self.sessions.pop(bot.bot_id,None)
    def state(self,bot):
        with self.lock:
            s=self.sessions.get(bot.bot_id)
            return {'manual':bool(s and getattr(bot,'manual_web_control',False)), 'status':str(getattr(bot,'status','')),
                    'chat':list(s['chat']) if s else [], 'window':dict(s['window']) if s and s['window'] else None}
    def chat(self,bot,text):
        if not getattr(bot,'manual_web_control',False) or not getattr(getattr(bot,'session',None),'connected',False):
            raise ValueError('ручная сессия не подключена')
        text=str(text).strip()
        if not text or len(text.encode('utf8'))>255:raise ValueError('сообщение: от 1 до 255 байт UTF-8')
        bot.api.sendchat(text)
        with self.lock:
            s=self.sessions.get(bot.bot_id)
            if s:s['chat'].append({'type':'out','text':text})
    def answer(self,bot,args):
        session=getattr(bot,'session',None)
        if not getattr(bot,'manual_web_control',False) or not session or not session.connected:raise ValueError('сессия не подключена')
        with self.lock:
            s=self.sessions.get(bot.bot_id);w=s and s.get('window')
            if not w or args.get('token')!=w['token']:raise ValueError('окно устарело или уже закрыто')
            if w['type']=='spawn':
                choice=int(args['choice'])
                if choice not in w['options']:raise ValueError('нет такого пункта')
                from logic.spawn import select_spawn
                sent=select_spawn(bot,choice,delay=0)
            elif w['type']=='dialog':
                response=int(args.get('response',1));item=int(args.get('item',0));text=str(args.get('text',''))
                if response not in (0,1) or not 0<=item<=65535 or len(text.encode('utf8'))>255:raise ValueError('некорректный ответ')
                sent=session.send_dialog_response(w['id'],response,item,text)
            elif w['type']=='npc':
                choice=int(args['choice'])
                if choice not in [b['key'] for b in w['buttons']]:raise ValueError('нет такой кнопки')
                sent=session.send_json(63,{'bk':choice},packet_name='interface_npc_dialog_response')
            else:raise ValueError('это окно пока не поддерживается')
            if not sent:raise ValueError('отправка не выполнена')
            s['window']=None
