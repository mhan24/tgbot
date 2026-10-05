"""Approved community airdrops: hash-ordered eligible winners, no points charged."""
import html
import json
import hashlib
import sqlite3
from chain import latest_block, BlockUnavailable, candidate_order
from points import CST
from datetime import datetime
import time

class Airdrops:
    def __init__(self,bot):
        self.bot=bot;self.db=bot.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS airdrops(
          id INTEGER PRIMARY KEY,source TEXT UNIQUE,actor INTEGER,minimum INTEGER,prize TEXT,
          candidates TEXT,cursor INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',
          message_id INTEGER,created INTEGER,actor_name TEXT,block_hash TEXT,block_height INTEGER,
          block_provider TEXT,candidate_digest TEXT,active_minutes INTEGER,
          winner_count INTEGER NOT NULL DEFAULT 1,block_chain TEXT NOT NULL DEFAULT 'Base',
          approved_by INTEGER,approver_name TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS airdrop_winners(
          airdrop_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,uid INTEGER NOT NULL,
          name TEXT,balance INTEGER,PRIMARY KEY(airdrop_id,ordinal),UNIQUE(airdrop_id,uid))''')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_airdrop_chain_block ON airdrops(block_chain,block_hash) WHERE block_hash IS NOT NULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS airdrop_grants(
          id INTEGER PRIMARY KEY,actor INTEGER,day TEXT,status TEXT NOT NULL DEFAULT 'pending',
          grantor INTEGER,created INTEGER,chat INTEGER,message_id INTEGER,minimum INTEGER,prize TEXT,
          source TEXT,response INTEGER,actor_name TEXT,active_minutes INTEGER,grantor_name TEXT,
          request_kind TEXT NOT NULL DEFAULT 'admin_extra',winner_count INTEGER NOT NULL DEFAULT 1)''')
        self.db.commit()

    def day(self, timestamp=None):
        return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, CST).date().isoformat()

    def owner(self, uid):
        return self.bot.call('getChatMember', chat_id=self.bot.group, user_id=uid)['status'] == 'creator'

    def used_today(self, uid, day=None):
        day = day or self.day()
        start = int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=CST).timestamp())
        return self.db.execute('SELECT count(*) FROM airdrops WHERE actor=? AND created>=? AND created<?',
                               (uid, start, start + 86400)).fetchone()[0]

    def display_name(self, user):
        return (' '.join(str(user.get(k) or '') for k in ('first_name','last_name')).strip()
                or user.get('username') or str(user['id']))[:128]

    def winners(self, aid):
        return self.db.execute('SELECT ordinal,uid,name,balance FROM airdrop_winners WHERE airdrop_id=? ORDER BY ordinal',
                               (aid,)).fetchall()

    def notify(self, grant, text):
        # Tell the requester where they asked, and in private when that is a different chat.
        try:self.bot.send(grant['chat'], text)
        except Exception:pass
        if grant['chat'] != grant['actor']:
            try:self.bot.send(grant['actor'], text)
            except Exception:pass

    def mark(self, grant, text):
        if not grant['response']:return
        try:self.bot.call('editMessageText', chat_id=self.bot.group, message_id=grant['response'],
                          text=text, parse_mode='HTML', reply_markup={'inline_keyboard':[]})
        except Exception:pass
        else:self.bot.schedule_delete(self.bot.group, grant['response'])

    def approval_shortcut(self, message_id):
        """Link a private status notice to its actionable approval card in the group."""
        chat_id=str(self.bot.group)
        if not message_id or not chat_id.startswith('-100'):
            return None
        return {'inline_keyboard': [[{
            'text': '打开群内审批卡',
            'url': f'https://t.me/c/{chat_id[4:]}/{message_id}',
        }]]}

    def history_command(self,msg,parts):
        chat=msg.get('chat',{});user=msg.get('from',{})
        if msg.get('sender_chat') or not user.get('id') or user.get('is_bot'):
            self.bot.send(chat.get('id'), '请使用个人账号查看空投历史。')
            return True
        try:
            member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=user['id'])
        except Exception:
            self.bot.send(chat['id'],'暂时无法核实你是否属于当前管理群，请稍后重试。')
            return True
        status=member.get('status')
        if not (status in ('creator','administrator','member') or
                (status=='restricted' and member.get('is_member'))):
            self.bot.send(chat['id'],'只有当前管理群成员可以查看空投历史。')
            return True
        if len(parts)>1:
            self.bot.send(chat['id'],'用法：/airdrops [空投编号]')
            return True
        safe=lambda value:html.escape(str(value or '').replace('@','＠'))
        if parts:
            try:aid=int(parts[0])
            except ValueError:
                self.bot.send(chat['id'],'空投编号必须是数字。用法：/airdrops [空投编号]')
                return True
            if aid<=0:
                self.bot.send(chat['id'],'空投编号必须是正整数。')
                return True
            row=self.db.execute('SELECT * FROM airdrops WHERE id=?',(aid,)).fetchone()
            if not row:
                self.bot.send(chat['id'],f'当前管理群找不到空投 #{aid}。')
                return True
            labels={'drawn':'已开奖','empty':'无合格中奖者','pending':'处理中'}
            created=datetime.fromtimestamp(row['created'],CST).strftime('%Y-%m-%d %H:%M:%S') if row['created'] else '未知'
            initiator=f'{safe(row["actor_name"] or row["actor"])}（ID：{row["actor"]}）'
            approver=(f'{safe(row["approver_name"] or row["approved_by"])}（ID：{row["approved_by"]}）'
                      if row['approved_by'] else '无需审批')
            winners=self.winners(row['id'])
            result_lines=[f'🗂 <b>空投 #{row["id"]}</b>',
                          f'状态：{labels.get(row["status"],safe(row["status"]))}',
                          f'创建时间：{created}',f'发起人：{initiator}',f'同意人：{approver}',
                          f'奖品：{safe(row["prize"])}',f'最低积分：{row["minimum"]}',
                          f'计划中奖人数：{row["winner_count"] or 1}']
            if row['active_minutes']:
                result_lines.append(f'活跃要求：{row["active_minutes"]} 分钟内发言')
            if winners:
                result_lines.append(f'中奖者：{len(winners)}/{row["winner_count"] or 1} 人')
                for winner in winners:
                    result_lines.append(f'{winner["ordinal"]}. {safe(winner["name"] or winner["uid"])}（ID：{winner["uid"]}，中奖时积分：{winner["balance"]}）')
            if row['block_hash']:
                result_lines.extend([f'区块链：{safe(row["block_chain"])}',
                                     f'区块高度：{row["block_height"]}',
                                     f'区块哈希：<code>{safe(row["block_hash"])}</code>',
                                     f'候选名单摘要：<code>{safe(row["candidate_digest"])}</code>'])
            self.bot.send(chat['id'],'\n'.join(result_lines),disable_web_page_preview=True)
            return True

        rows=self.db.execute('SELECT id,created,status,actor,actor_name,approved_by,approver_name,prize,winner_count '
                             'FROM airdrops ORDER BY created DESC,id DESC LIMIT 20').fetchall()
        if not rows:
            self.bot.send(chat['id'],'当前管理群还没有空投记录。')
            return True
        labels={'drawn':'已开奖','empty':'无合格中奖者','pending':'处理中'}
        lines=['🗂 <b>最近 20 条空投记录</b>']
        for row in rows:
            created=datetime.fromtimestamp(row['created'],CST).strftime('%m-%d %H:%M') if row['created'] else '未知'
            initiator=safe(str(row['actor_name'] or row['actor'])[:32])
            prize=safe(str(row['prize'] or '')[:50])
            status=labels.get(row['status'],safe(row['status']))
            line=f'• <code>#{row["id"]}</code> · {created} · {status} · {prize}\n  发起人：{initiator}（ID：{row["actor"]}）'
            if row['approved_by']:
                line+=f' · 同意人：{safe(str(row["approver_name"] or row["approved_by"])[:32])}（ID：{row["approved_by"]}）'
            winners=self.winners(row['id'])
            if winners:
                line+=f'\n  中奖人数：{len(winners)}/{row["winner_count"] or 1}'
            lines.append(line)
        lines.extend(['','查看单条详情：<code>/airdrops 编号</code>'])
        self.bot.send(chat['id'],'\n'.join(lines),disable_web_page_preview=True)
        return True

    def command(self,msg):
        text_content=msg.get('text','').strip()
        parts=text_content.split()
        if not parts:return False
        cmd=parts[0].split('@',1)
        if cmd[0].lower() == '/airdrops' and (len(cmd)==1 or cmd[1].lower()==self.bot.username.lower()):
            return self.history_command(msg,parts[1:])
        if cmd[0].lower()!='/airdrop' or (len(cmd)>1 and cmd[1].lower()!=self.bot.username.lower()):return False
        chat=msg['chat']['id'];user=msg.get('from',{})
        if msg.get('sender_chat') or not user.get('id') or user.get('is_bot'):
            self.bot.send(chat,'请使用个人账号发起空投申请。');return True
        
        # Format: /airdrop <minimum> [active_minutes] [winner_count] <prize>.
        minimum = None
        active_minutes = None
        winner_count = 1
        prize = ''
        if len(parts) >= 3 and parts[1].isdigit() and 0 <= int(parts[1]) <= 1000000:
            minimum = int(parts[1])
            tail = parts[2:]
            duration = tail[0][:-1] if tail and tail[0].lower().endswith('m') else (tail[0] if tail else '')
            if duration.isdigit() and int(duration) <= 1000000:
                active_minutes = int(duration) or None
                if len(tail) >= 3 and tail[1].isdigit():
                    winner_count = int(tail[1])
                    prize = ' '.join(tail[2:]).strip()
                else:
                    prize = ' '.join(tail[1:]).strip()
            else:
                prize = ' '.join(tail).strip()
        
        if minimum is None or not 1<=winner_count<=20 or not prize or not 1<=len(prize)<=200:
            self.bot.send(chat,'用法：/airdrop 最低积分 [活跃分钟] [中奖人数] 奖品名称\n例如：/airdrop 50 840 5 奖品（最低50分、840分钟内发言、抽5人）；不限制活跃时写 0。默认抽1人，最多20人。普通成员每天可申请一次，需管理员同意；管理员每天可直接发起一次，群主不限。');return True
        source=f'{chat}:{msg["message_id"]}'
        row=self.db.execute('SELECT * FROM airdrops WHERE source=?',(source,)).fetchone()
        if not row:
            uid=user['id']
            try:
                member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=uid)
                status=member.get('status')
                is_admin=status in ('creator','administrator')
                is_owner=status=='creator'
                is_member=(status in ('creator','administrator','member') or
                           (status=='restricted' and member.get('is_member')))
            except Exception:
                self.bot.send(chat,'暂时无法确认你在群内的身份，请稍后重试。');return True
            if not is_member:
                self.bot.send(chat,'只有当前管理群成员可以发起空投。')
                return True
            if not is_admin:
                day=self.day()
                prior=self.db.execute('SELECT * FROM airdrop_grants WHERE actor=? AND day=? ORDER BY id DESC LIMIT 1',
                                      (uid,day)).fetchone()
                if self.used_today(uid,day) or prior:
                    if prior and prior['status']=='pending':
                        self.bot.send(chat,'你的空投申请今天已提交，正在等待管理员处理。')
                    else:
                        self.bot.send(chat,'每位成员每天最多申请一次空投。')
                    return True
                self._request_approval(msg,user,minimum,prize,source,active_minutes,'member',winner_count)
                return True
            grant_id=None
            if not is_owner and self.used_today(uid):
                day=self.day()
                grant=self.db.execute("SELECT * FROM airdrop_grants WHERE actor=? AND day=? AND request_kind='admin_extra' AND status='approved' ORDER BY id LIMIT 1",(uid,day)).fetchone()
                if not grant:
                    pending=self.db.execute("SELECT response FROM airdrop_grants WHERE actor=? AND day=? AND request_kind='admin_extra' AND status='pending' ORDER BY id DESC LIMIT 1",(uid,day)).fetchone()
                    if pending:
                        markup=self.approval_shortcut(pending['response'])
                        self.bot.send(chat,'你的额外空投申请正在等待群主批准。',reply_markup=markup)
                        return True
                    self._request_approval(msg,user,minimum,prize,source,active_minutes,'admin_extra',winner_count)
                    return True
                grant_id=grant['id']
            if not self.create(source,user,minimum,prize,chat,grant_id,active_minutes,winner_count=winner_count):return True
            row=self.db.execute('SELECT * FROM airdrops WHERE source=?',(source,)).fetchone()
        return self.finalize(row,chat)

    def _request_approval(self,msg,user,minimum,prize,source,active_minutes,request_kind,winner_count=1):
        chat=msg['chat']['id'];day=self.day();uid=user['id']
        duplicate=False
        with self.db:
            # The bot has a single update poller; keeping this check and insert in one
            # transaction reserves the requester's daily slot before notifying admins.
            if request_kind=='member':
                prior=self.db.execute('SELECT id FROM airdrop_grants WHERE actor=? AND day=? LIMIT 1',
                                      (uid,day)).fetchone()
                if prior or self.used_today(uid,day):
                    duplicate=True
            if not duplicate:
                cursor=self.db.execute("INSERT INTO airdrop_grants(actor,day,status,created,chat,message_id,minimum,prize,source,actor_name,active_minutes,request_kind,winner_count) VALUES(?,?,'pending',?,?,?,?,?,?,?,?,?,?)",
                                       (uid,day,int(time.time()),chat,msg['message_id'],minimum,prize,source,
                                        self.display_name(user),active_minutes,request_kind,winner_count))
                grant_id=cursor.lastrowid
        if duplicate:
            self.bot.send(chat,'每位成员每天最多申请一次空投。')
            return None
        safe=lambda value:html.escape(str(value).replace('@','＠'))
        initiator=f'<a href="tg://user?id={uid}">{safe(self.display_name(user))}</a>'
        permission='群管理员' if request_kind=='member' else '群主'
        req_text=f'🎁 空投申请 #{grant_id}\n发起人：{initiator}（ID：{uid}）\n奖品：{safe(prize)}\n最低积分：{minimum}\n中奖人数：{winner_count}'
        if active_minutes is not None:req_text+=f'\n活跃条件：{active_minutes} 分钟内发过言'
        req_text+=f'\n\n请{permission}审核；同意后会立即开奖。'
        try:
            result=self.bot.send(self.bot.group,req_text,
                reply_markup={'inline_keyboard':[[
                    {'text':'同意并开奖','callback_data':f'a:ok:{grant_id}'},
                    {'text':'拒绝','callback_data':f'a:no:{grant_id}'}]]},keep=True)
        except Exception:
            with self.db:
                self.db.execute("DELETE FROM airdrop_grants WHERE id=? AND status='pending'",(grant_id,))
            raise
        with self.db:
            self.db.execute('UPDATE airdrop_grants SET response=? WHERE id=?',(result['message_id'],grant_id))
        if request_kind=='member':
            notice='✅ 空投申请已提交，等待群管理员同意。批准后会自动开奖，每人每天限一次。'
        else:
            notice='你的管理员每日额度已用完，已向群主申请额外授权。批准后会自动开奖。'
        markup=self.approval_shortcut(result['message_id']) if chat!=self.bot.group else None
        self.bot.send(chat,notice,reply_markup=markup)
        return grant_id

    def create(self,source,user,minimum,prize,chat,grant_id=None,active_minutes=None,
               approved_by=None,approver_name=None,winner_count=1):
        actor_name=self.display_name(user)
        if active_minutes is not None:
            cutoff = int(time.time()) - active_minutes * 60
            ids=[r[0] for r in self.db.execute('SELECT pu.uid FROM point_users pu JOIN user_activity ua ON pu.uid=ua.uid WHERE pu.total>=? AND pu.uid!=? AND ua.last_active>=? ORDER BY pu.uid',(minimum,self.bot.bot_id,cutoff))]
        else:
            ids=[r[0] for r in self.db.execute('SELECT uid FROM point_users WHERE total>=? AND uid!=? ORDER BY uid',(minimum,self.bot.bot_id))]
        try:
            block=latest_block()
        except BlockUnavailable as exc:
            self.bot.send(chat,str(exc));return False
        if self.db.execute('SELECT id FROM airdrops WHERE block_chain=? AND block_hash=?',(block['chain'],block['hash'])).fetchone():
            self.bot.send(chat,f'当前 Base 区块已用于空投，请等待新区块产生后再创建。');return False
        digest=hashlib.sha256(json.dumps(sorted(ids),separators=(',',':')).encode('ascii')).hexdigest()
        ids=candidate_order(block['hash'],ids)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if grant_id is not None:
                if self.db.execute("UPDATE airdrop_grants SET status='used' WHERE id=? AND status='approved'",(grant_id,)).rowcount!=1:
                    self.db.rollback();self.bot.send(chat,'今日额度已用完，等待群主授权后再发送 /airdrop。');return False
            self.db.execute('INSERT INTO airdrops(source,actor,minimum,prize,candidates,created,actor_name,block_hash,block_height,block_provider,candidate_digest,block_chain,active_minutes,approved_by,approver_name,winner_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (source,user['id'],minimum,prize,json.dumps(ids),int(time.time()),actor_name,block['hash'],block['height'],block['provider'],digest,block['chain'],active_minutes,approved_by,approver_name,winner_count))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            self.bot.send(chat,'此命令或 Base 区块已经创建过空投，请勿重复创建。');return False
        except Exception:
            self.db.rollback();raise
        return True

    def finalize(self,row,chat):
        aid=row['id']
        if row['status']=='pending':
            # Hash-ranked candidates + the requested number of distinct winners.
            # Persist the block/order/cursor so retries never change the draw.
            ids=json.loads(row['candidates'])
            target=max(1,int(row['winner_count'] or 1))
            winner_total=self.db.execute('SELECT count(*) FROM airdrop_winners WHERE airdrop_id=?',(aid,)).fetchone()[0]
            for i in range(row['cursor'],len(ids)):
                if winner_total>=target:break
                uid=ids[i]
                balance=self.db.execute('SELECT total FROM point_users WHERE uid=?',(uid,)).fetchone()
                pending=self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'",(uid,)).fetchone()
                eligible=bool(balance and balance[0]>=row['minimum'] and not pending and uid!=self.bot.bot_id)
                member=None
                if eligible:
                    # An API failure aborts/retries instead of treating an unknown user as ineligible.
                    member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=uid)
                    u=member.get('user',{})
                    eligible=u.get('id')==uid and u.get('is_bot') is False and (
                        member['status'] in ('member','administrator','creator') or
                        (member['status']=='restricted' and member.get('is_member')))
                    # A tag or custom title is required even when the points floor is met.
                    if eligible:
                        eligible=bool(str(member.get('tag') or '').strip() or str(member.get('custom_title') or '').strip())
                with self.db:
                    self.db.execute('UPDATE airdrops SET cursor=? WHERE id=?',(i+1,aid))
                    if eligible:
                        u=member['user'];name=' '.join(str(u.get(k) or '') for k in ('first_name','last_name')).strip() or str(uid)
                        ordinal=winner_total+1
                        inserted=self.db.execute('INSERT OR IGNORE INTO airdrop_winners(airdrop_id,ordinal,uid,name,balance) VALUES(?,?,?,?,?)',
                                                 (aid,ordinal,uid,name[:128],balance[0])).rowcount
                        if inserted:
                            winner_total+=1
            with self.db:
                if winner_total:
                    self.db.execute("UPDATE airdrops SET status='drawn' WHERE id=?",(aid,))
                elif self.db.execute('SELECT cursor FROM airdrops WHERE id=?',(aid,)).fetchone()[0] >= len(ids):
                    self.db.execute("UPDATE airdrops SET status='empty' WHERE id=?",(aid,))
            row=self.db.execute('SELECT * FROM airdrops WHERE id=?',(aid,)).fetchone()
        if row['message_id']:return True
        safe=lambda v:html.escape(str(v).replace('@','＠'))
        initiator=f'<a href="tg://user?id={row["actor"]}">{safe(row["actor_name"] or row["actor"])}</a>（ID：{row["actor"]}）'
        approver=(f'\n同意人：<a href="tg://user?id={row["approved_by"]}">{safe(row["approver_name"] or row["approved_by"])}</a>（ID：{row["approved_by"]}）'
                  if row['approved_by'] else '')
        proof=(f'\n\n{row["block_chain"]} 区块高度：{row["block_height"]}\n唯一哈希：<code>{row["block_hash"]}</code>\n候选名单摘要：<code>{row["candidate_digest"]}</code>' if row['block_hash'] else '')
        active_text = f'及 {row["active_minutes"]} 分钟内活跃' if row['active_minutes'] else ''
        winners=self.winners(aid)
        if row['status']=='empty':
            result=self.bot.send(chat,f'空投 #{aid}\n发起人：{initiator}{approver}\n未开奖：没有找到同时满足 {row["minimum"]} 分门槛{active_text}、且已设置成员标签或头衔的在群用户。{proof}',
                                 keep=chat==self.bot.group)
        else:
            active_info = f'\n活跃要求：{row["active_minutes"]} 分钟内发言' if row['active_minutes'] else ''
            winner_lines=[]
            for winner in winners:
                name=safe(str(winner['name'] or winner['uid'])[:32])
                winner_lines.append(f'{winner["ordinal"]}. <a href="tg://user?id={winner["uid"]}">{name}</a>（ID：{winner["uid"]}，积分：{winner["balance"]}）')
            result=self.bot.send(self.bot.group,
                f'🎉 <b>空投开奖 #{aid}</b>\n发起人：{initiator}{approver}\n奖品：{safe(row["prize"])}\n'
                f'最低积分：{row["minimum"]}{active_info}\n中奖人数：{len(winners)}/{row["winner_count"] or 1}\n\n'
                f'中奖者：\n'+'\n'.join(winner_lines)+
                f'\n\n本次不扣积分。请中奖者联系管理员领取奖品。{proof}',keep=True)
        with self.db:self.db.execute('UPDATE airdrops SET message_id=? WHERE id=?',(result['message_id'],aid))
        if chat!=self.bot.group and row['status']=='drawn':self.bot.send(chat,f'空投 #{aid} 已开奖，结果已发布到群。')
        return True

    def callback(self,q):
        data=q.get('data','').split(':')
        if len(data)!=3 or data[0]!='a' or data[1] not in ('ok','no') or not data[2].isdigit():
            return False
        def answer(text):self.bot.call('answerCallbackQuery',callback_query_id=q['id'],text=text,show_alert=True)
        grant=self.db.execute('SELECT * FROM airdrop_grants WHERE id=?',(int(data[2]),)).fetchone()
        if not grant:
            answer('空投申请不存在或已过期。');return True
        message=q.get('message',{})
        if message.get('chat',{}).get('id')!=self.bot.group or message.get('message_id')!=grant['response']:
            answer('请在群内原始空投申请消息上操作。');return True
        approver=q.get('from',{})
        try:
            is_admin=self.bot.admin(approver['id'])
            is_owner=self.owner(approver['id']) if is_admin else False
        except Exception:
            answer('暂时无法确认你的群内权限。');return True
        if not is_admin:
            answer('仅本群管理员可以处理空投申请。');return True
        if grant['request_kind']=='admin_extra' and not is_owner:
            answer('管理员额度外的空投仍需群主批准。');return True
        if grant['status'] in ('approved','used'):
            row=self.db.execute('SELECT * FROM airdrops WHERE source=?',(grant['source'],)).fetchone()
            if row:
                chat=grant['chat'] if grant['chat'] is not None else self.bot.group
                self.finalize(row,chat)
                answer('已恢复处理该空投。')
                return True
        if grant['status']!='pending':
            answer('该请求已经处理过。');return True
        approver_name=self.display_name(approver)
        if data[1]=='no':
            with self.db:
                self.db.execute("UPDATE airdrop_grants SET status='denied',grantor=?,grantor_name=? WHERE id=? AND status='pending'",
                                (approver['id'],approver_name,grant['id']))
            safe=html.escape(approver_name)
            self.mark(grant,f'❌ 已由管理员 <a href="tg://user?id={approver["id"]}">{safe}</a> 拒绝。')
            self.notify(grant,f'管理员 {approver_name} 已拒绝你的空投申请。')
            answer('已拒绝该空投申请。')
            return True
        if not (grant['source'] and grant['minimum'] is not None and grant['prize']):
            answer('该请求缺少参数，请重新发起 /airdrop。');return True
        with self.db:
            if self.db.execute("UPDATE airdrop_grants SET status='approved',grantor=?,grantor_name=? WHERE id=? AND status='pending'",
                               (approver['id'],approver_name,grant['id'])).rowcount!=1:
                answer('该请求已经处理过。');return True
        chat=grant['chat'] if grant['chat'] is not None else self.bot.group
        user={'id':grant['actor'],'first_name':grant['actor_name'] or ''}
        row=self.db.execute('SELECT * FROM airdrops WHERE source=?',(grant['source'],)).fetchone()
        if not row:
            if not self.create(grant['source'],user,grant['minimum'],grant['prize'],chat,grant['id'],
                               grant['active_minutes'],approver['id'],approver_name,
                               winner_count=grant['winner_count'] or 1):
                with self.db:
                    self.db.execute("UPDATE airdrop_grants SET status='pending',grantor=NULL,grantor_name=NULL WHERE id=? AND status='approved'",
                                    (grant['id'],))
                self.notify(grant,'管理员已同意，但当前无法开奖。申请仍保留，稍后可再次由管理员批准。')
                answer('暂时无法开奖；申请已保留，可稍后再次批准。')
                return True
            row=self.db.execute('SELECT * FROM airdrops WHERE source=?',(grant['source'],)).fetchone()
        answer('已同意，正在执行空投。')
        self.finalize(row,chat)
        self.mark(grant,f'✅ 已由管理员 <a href="tg://user?id={approver["id"]}">{html.escape(approver_name)}</a> 同意，空投已执行。')
        self.notify(grant,f'管理员 {approver_name} 已同意，你发起的空投现已执行。')
        return True
