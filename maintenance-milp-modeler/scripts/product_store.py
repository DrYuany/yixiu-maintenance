"""Transactional local product state. Planned reservations never deduct physical stock."""
import getpass
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from product_data import Snapshot, digest, load_project
from product_planner import verify_schedule, account


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            INSERT OR IGNORE INTO settings VALUES('revision',0);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,result TEXT NOT NULL,snapshot TEXT NOT NULL,path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,state TEXT NOT NULL,
              payload TEXT NOT NULL,creator TEXT NOT NULL,approver TEXT,FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS ownership(task_id TEXT PRIMARY KEY,order_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations(task_id TEXT PRIMARY KEY,order_id TEXT,part TEXT,at_minute INTEGER,state TEXT);
            CREATE TABLE IF NOT EXISTS documents(order_id TEXT,kind TEXT,payload TEXT,PRIMARY KEY(order_id,kind));
            CREATE TABLE IF NOT EXISTS postings(id TEXT PRIMARY KEY,part TEXT,delta INTEGER,at TEXT);
            CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY,action TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,actor TEXT,action TEXT,payload TEXT,previous TEXT,hash TEXT);
            CREATE TABLE IF NOT EXISTS learning(id TEXT PRIMARY KEY,state TEXT,payload TEXT);
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA journal_mode=WAL')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback();raise
        finally: db.close()

    def revision(self,db):
        return db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0]

    def event(self,db,actor,action,payload):
        last=db.execute('SELECT hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        previous=last[0] if last else ''
        at=datetime.now(timezone.utc).isoformat()
        value={'at':at,'actor':actor,'action':action,'payload':payload,'previous':previous}
        db.execute('INSERT INTO events(at,actor,action,payload,previous,hash) VALUES(?,?,?,?,?,?)',
                   (at,actor,action,encode(payload),previous,digest(value)))

    def overlay(self,data):
        with self.connection() as db:
            data.meta['业务版本']=self.revision(db)
            posted=set()
            for pid,part,delta,at in db.execute('SELECT id,part,delta,at FROM postings'):
                if data.minute(at)<=data.now:
                    data.stocks[part]+=delta
                    posted.add(pid)
                    if pid.startswith('issue:'):data.issued.append(pid.removeprefix('issue:'))
            data.movements=[m for m in data.movements if m['时间']>data.now and 'external:'+m['编号'] not in posted]
            completed=set()
            for state,payload in db.execute("SELECT state,payload FROM orders WHERE state IN ('已批准','已领料','已完成')"):
                p=json.loads(payload)
                known={t['任务编号'] for t in data.tasks}
                for row in p['任务']:
                    if row['任务编号'] not in known:
                        raise ValueError('已有批准任务不在本次数据快照，必须先对账')
                    if state=='已完成':completed.add(row['任务编号'])
                    else:data.fixed.append(row)
            data.tasks=[t for t in data.tasks if t['任务编号'] not in completed]
        return data

    def save_run(self,report,data,path):
        run_id='RUN-'+uuid.uuid4().hex[:12]
        with self.connection() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?)',(run_id,encode(report),encode(data.__dict__),str(Path(path).resolve())))
            self.event(db,getpass.getuser(),'保存推荐',{'运行编号':run_id,'输入哈希':data.meta['输入哈希']})
        return run_id

    def latest_run_id(self):
        with self.connection() as db:
            row=db.execute('SELECT id FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
            if not row: raise ValueError('还没有生成过推荐方案，请先调用生成检修窗口推荐')
            return row[0]

    def replay(self,db,key,action):
        row=db.execute('SELECT action,result FROM receipts WHERE key=?',(key,)).fetchone()
        if row:
            if row[0]!=action: raise ValueError('幂等键已用于另一操作或不同参数')
            return json.loads(row[1])

    def receipt(self,db,key,action,result):
        db.execute('INSERT INTO receipts VALUES(?,?,?)',(key,action,encode(result)))
        return result

    def order(self,db,order_id):
        row=db.execute('SELECT run_id,state,payload,creator,approver FROM orders WHERE id=?',(order_id,)).fetchone()
        if not row: raise ValueError('检修单不存在')
        return row

    def draft(self,run_id,windows=None,key=None,actor=None,accept_partial=False):
        actor=actor or getpass.getuser();key=key or str(uuid.uuid4())
        action='生成检修单:'+digest([run_id,windows,accept_partial])
        with self.connection() as db:
            cached=self.replay(db,key,action)
            if cached:return cached
            row=db.execute('SELECT result,snapshot FROM runs WHERE id=?',(run_id,)).fetchone()
            if not row: raise ValueError('运行不存在')
            report=json.loads(row[0]);snap=json.loads(row[1])
            if '推荐' not in report or not report['推荐']['复核通过']: raise ValueError('无有效推荐，不能生成检修单')
            if report['求解'].get('诊断模型') and not accept_partial: raise ValueError('部分诊断方案需明确接受未完成任务后才可送审')
            if snap['meta'].get('业务版本',0)!=self.revision(db): raise ValueError('业务版本已变化，请重新规划')
            owned={r[0] for r in db.execute('SELECT task_id FROM ownership')}
            tasks=[s for s in report['推荐']['排程'] if s['任务编号'] not in owned and (not windows or s['窗口编号'] in windows)]
            if not tasks: raise ValueError('没有可生成的新检修任务')
            if windows and set(windows)-{s['窗口编号'] for s in report['推荐']['排程']}: raise ValueError('所选窗口没有推荐任务')
            oid='WO-'+uuid.uuid4().hex[:12]
            payload={'任务':tasks,'数据性质':report['数据']['数据性质'],'输入哈希':report['数据']['输入哈希'],
                     '业务版本':snap['meta'].get('业务版本',0),'接受部分方案':accept_partial}
            db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?)',(oid,run_id,'待审批',encode(payload),actor,None))
            self.event(db,actor,'生成待审批检修单',{'检修单号':oid,'任务数':len(tasks)})
            return self.receipt(db,key,action,{'检修单号':oid,'状态':'待审批','任务数':len(tasks)})

    def approve(self,oid,project,key=None,actor=None,reject=False,input_file=None):
        actor=actor or getpass.getuser();key=key or str(uuid.uuid4());action='审批:'+digest([oid,reject,actor])
        with self.connection() as db:
            cached=self.replay(db,key,action)
            if cached:return cached
            run_id,state,raw,creator,_=self.order(db,oid);payload=json.loads(raw)
            if state!='待审批': raise ValueError('只有待审批检修单可以审批')
            if not reject and actor==creator: raise ValueError('审批人不能与制单人相同')
            row=db.execute('SELECT snapshot FROM runs WHERE id=?',(run_id,)).fetchone()
            snap=json.loads(row[0]);meta=snap['meta']
            if reject:
                db.execute("UPDATE orders SET state='已拒绝',approver=? WHERE id=?",(actor,oid))
            else:
                current=load_project(project,meta.get('场景','基座01'),meta['规划时点'],meta.get('参数覆盖'),input_file=input_file)
                if current.meta['输入哈希']!=payload['输入哈希'] or self.revision(db)!=payload['业务版本']:
                    raise ValueError('数据或库存/审批状态已变化，旧推荐不能审批，请重新规划')
                snap['base']=datetime.fromisoformat(snap['base']);data=Snapshot(**snap)
                verify_schedule(data,data.fixed+payload['任务'])
                for s in payload['任务']:
                    db.execute('INSERT INTO ownership VALUES(?,?)',(s['任务编号'],oid))
                    db.execute('INSERT INTO reservations VALUES(?,?,?,?,?)',(s['任务编号'],oid,s['部件编号'],s['开始'],'已预留'))
                departure=[{'任务编号':s['任务编号'],'风机':s['风机编号'],'部件':s['部件名称'],
                    '备件数量':1,'人员需求':1,'作业线':s['作业线'],'开工时间':data.stamp(s['开始']),
                    '完工时间':data.stamp(s['完成']),'有效人时':sum(b-a for a,b in s['作业段'])/60} for s in payload['任务']]
                changes=[{'任务编号':s['任务编号'],'部件编号':s['部件编号'],'数量':-1,
                          '计划消耗时间':data.stamp(s['开始']),'性质':'审批预留，尚未实物扣减'} for s in payload['任务']]
                for name,doc in [('出海清单',departure),('库存变化单',changes)]:
                    db.execute('INSERT INTO documents VALUES(?,?,?)',(oid,name,encode(doc)))
                db.execute("UPDATE orders SET state='已批准',approver=? WHERE id=?",(actor,oid))
                db.execute("UPDATE settings SET value=value+1 WHERE key='revision'")
            result={'检修单号':oid,'状态':'已拒绝' if reject else '已批准','实物库存已扣减':False}
            self.event(db,actor,'拒绝检修单' if reject else '批准并预留库存',result)
            return self.receipt(db,key,action,result)

    def post_external(self,run_id,movement_id,at,key,actor=None):
        actor=actor or getpass.getuser();action='确认实际收发:'+digest([run_id,movement_id,at])
        with self.connection() as db:
            cached=self.replay(db,key,action)
            if cached:return cached
            row=db.execute('SELECT snapshot FROM runs WHERE id=?',(run_id,)).fetchone()
            if not row:raise ValueError('运行不存在')
            snap=json.loads(row[0]);snap['base']=datetime.fromisoformat(snap['base']);data=Snapshot(**snap)
            movement=next((m for m in data.movements if m['编号']==movement_id),None)
            if not movement:raise ValueError('确认流水不存在')
            if data.minute(at)<movement['时间']:raise ValueError('提前到货须先更新输入版本，不能直接套用原计划')
            delta=movement['数量']*(1 if movement['方向']=='补库' else -1)
            balance=data.stocks[movement['部件编号']]+db.execute('SELECT COALESCE(SUM(delta),0) FROM postings WHERE part=?',(movement['部件编号'],)).fetchone()[0]
            if balance+delta<0:raise ValueError('实际出库将导致负库存')
            db.execute('INSERT INTO postings VALUES(?,?,?,?)',('external:'+movement_id,movement['部件编号'],delta,at))
            db.execute("UPDATE settings SET value=value+1 WHERE key='revision'")
            result={'流水编号':movement_id,'实际数量变化':delta}
            self.event(db,actor,'确认实际库存收发',result)
            return self.receipt(db,key,action,result)

    def execute(self,oid,action_name,key,actor=None,at=None):
        actor=actor or getpass.getuser();action=action_name+':'+digest([oid,at])
        with self.connection() as db:
            cached=self.replay(db,key,action)
            if cached:return cached
            run_id,state,raw,_,_=self.order(db,oid);payload=json.loads(raw)
            snap=json.loads(db.execute('SELECT snapshot FROM runs WHERE id=?',(run_id,)).fetchone()[0])
            snap['base']=datetime.fromisoformat(snap['base']);data=Snapshot(**snap)
            if at is None:raise ValueError('执行反馈必须给出实际发生时间')
            moment=data.minute(at)
            if action_name=='领料':
                if state!='已批准':raise ValueError('只有已批准检修单可领料')
                counts=default_counts(payload['任务'])
                for part,n in counts.items():
                    actual=data.stocks[part]+db.execute('SELECT COALESCE(SUM(delta),0) FROM postings WHERE part=?',(part,)).fetchone()[0]
                    outstanding=sum(m['数量'] for m in data.movements if m['部件编号']==part and m['方向']=='出库' and m['时间']<=moment
                                    and not db.execute('SELECT 1 FROM postings WHERE id=?',('external:'+m['编号'],)).fetchone())
                    if actual-outstanding<n:raise ValueError('实际可用库存不足；计划补货不能替代实际收货确认')
                for s in payload['任务']:
                    db.execute('INSERT INTO postings VALUES(?,?,?,?)',('issue:'+s['任务编号'],s['部件编号'],-1,at))
                db.execute("UPDATE reservations SET state='已领料' WHERE order_id=?",(oid,));next_state='已领料'
            elif action_name=='完成':
                if state!='已领料':raise ValueError('完成反馈前应先记录实际领料')
                if moment<max(s['开始'] for s in payload['任务']):raise ValueError('完成时间早于计划任务开始')
                next_state='已完成'
                db.execute('INSERT INTO documents VALUES(?,?,?)',(oid,'执行反馈',encode({'实际完成时间':at,'反馈人':actor})))
            elif action_name=='取消':
                if state not in ('待审批','已批准'):raise ValueError('已领料/执行中的任务需退料与执行变更，不能直接取消')
                if state=='已批准' and moment>=min(s['开始'] for s in payload['任务']):raise ValueError('已进入作业期需执行变更，不能取消冻结计划')
                db.execute('DELETE FROM ownership WHERE order_id=?',(oid,));db.execute('DELETE FROM reservations WHERE order_id=?',(oid,));next_state='已取消'
            else:raise ValueError('未知执行动作')
            db.execute('UPDATE orders SET state=? WHERE id=?',(next_state,oid))
            db.execute("UPDATE settings SET value=value+1 WHERE key='revision'")
            result={'检修单号':oid,'状态':next_state}
            self.event(db,actor,action_name,result)
            return self.receipt(db,key,action,result)

    def list_orders(self):
        with self.connection() as db:
            return [{'检修单号':oid,'运行编号':rid,'状态':state,'创建人':creator,'审批人':approver,'内容':json.loads(payload)}
                    for oid,rid,state,payload,creator,approver in db.execute('SELECT * FROM orders ORDER BY rowid DESC')]

    def documents(self,oid):
        with self.connection() as db:
            return {kind:json.loads(payload) for kind,payload in db.execute('SELECT kind,payload FROM documents WHERE order_id=?',(oid,))}

    def audit(self):
        with self.connection() as db:
            previous='';count=0
            for at,actor,action,payload,prev,hash_value in db.execute('SELECT at,actor,action,payload,previous,hash FROM events ORDER BY seq'):
                value={'at':at,'actor':actor,'action':action,'payload':json.loads(payload),'previous':prev}
                if previous!=prev or digest(value)!=hash_value:raise ValueError('审计链校验失败')
                previous=hash_value;count+=1
            return {'审计链通过':True,'事件数':count,'业务版本':self.revision(db)}


def default_counts(tasks):
    result={}
    for t in tasks:result[t['部件编号']]=result.get(t['部件编号'],0)+1
    return result
