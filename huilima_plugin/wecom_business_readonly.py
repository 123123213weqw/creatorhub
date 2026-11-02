"""Allowlisted business-history reads, restricted to one visible test member.

Official contracts downloaded/verified: document/path/92120, /93338, /93333.
All POSTs below are documented read operations. No create/send/detail/chat API.
IDs, message bodies, attachment URLs and cursors never leave this client.
"""
from __future__ import annotations
import json,time
import httpx
from .wecom_customer_readonly import WeComCustomerReadOnly
from .wecom_readonly import API,MAX_RESPONSE,WeComError

ROUTES={
    '/externalcontact/groupchat/list',
    '/externalcontact/get_groupmsg_list_v2',
    '/externalcontact/get_groupmsg_task',
    '/externalcontact/get_groupmsg_send_result',
    '/externalcontact/get_moment_list',
    '/externalcontact/get_moment_task',
}
MAX_PAGES=3
MAX_TASKS=10
MAX_REQUESTS=36


def identifier(value):
    if (not isinstance(value,str) or not 0<len(value)<=512
            or any(ord(c)<33 or ord(c)==127 for c in value)):
        raise WeComError('wecom_business_response_invalid')
    return value


class WeComBusinessReadOnly(WeComCustomerReadOnly):
    def _read_post(self,route,body):
        if route not in ROUTES:raise WeComError('wecom_route_rejected')
        if self._requests>=MAX_REQUESTS or time.monotonic()>self._deadline:
            raise WeComError('wecom_business_request_budget')
        for attempt in range(2):
            self._requests+=1
            if self._requests>MAX_REQUESTS or time.monotonic()>self._deadline:
                raise WeComError('wecom_business_request_budget')
            try:
                with self._http.stream('POST',API+route,params={'access_token':self._access_token()},json=body) as response:
                    if response.status_code in {429,500,502,503,504} and attempt==0:
                        self._sleep(.5);continue
                    if response.status_code!=200:raise WeComError('wecom_business_http_rejected')
                    raw=bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw)>MAX_RESPONSE:raise WeComError('wecom_response_too_large')
                    data=json.loads(raw)
                if not isinstance(data,dict) or type(data.get('errcode')) is not int:
                    raise WeComError('wecom_business_response_invalid')
                if data['errcode']:
                    if data['errcode'] in {40014,42001} and attempt==0:
                        self._token,self._expires='',0.;continue
                    raise WeComError('wecom_business_api_rejected',data['errcode'])
                return data
            except (httpx.TimeoutException,httpx.NetworkError):
                if attempt==0:self._sleep(.5);continue
                raise WeComError('wecom_business_network_unavailable') from None
            except (httpx.HTTPError,ValueError):raise WeComError('wecom_business_response_invalid') from None
        raise WeComError('wecom_business_read_unavailable')

    def _pages(self,route,body,key,identity):
        result=[];seen=set();ids=set();cursor=''
        for _ in range(MAX_PAGES):
            data=self._read_post(route,{**body,**({'cursor':cursor} if cursor else {})})
            rows=data.get(key)
            if not isinstance(rows,list) or len(rows)>body['limit']:
                raise WeComError('wecom_business_response_invalid')
            for row in rows:
                if not isinstance(row,dict):raise WeComError('wecom_business_response_invalid')
                rid=identifier(identity(row))
                if rid in ids:raise WeComError('wecom_business_duplicate_record')
                ids.add(rid);result.append(row)
            cursor=data.get('next_cursor','')
            if cursor=='':return result,True
            if not isinstance(cursor,str) or len(cursor)>4096 or cursor in seen:
                raise WeComError('wecom_business_pagination_invalid')
            seen.add(cursor)
        return result,False

    def _member(self):
        app=self._authenticated('/agent/get')
        scopes=[app.get(k,{}) for k in ('allow_userinfos','allow_partys','allow_tags')]
        if (type(app.get('agentid')) is not int or app['agentid']!=self.credentials.agent_id
                or type(app.get('close')) is not int or app['close']!=0
                or any(not isinstance(x,dict) for x in scopes)):
            raise WeComError('wecom_application_mismatch')
        users=scopes[0].get('user',[])
        if (not isinstance(users,list) or len(users)!=1 or not isinstance(users[0],dict)
                or scopes[1].get('partyid',[])!=[] or scopes[2].get('tagid',[])!=[]):
            raise WeComError('wecom_single_test_member_scope_required')
        return identifier(users[0].get('userid'))

    @staticmethod
    def _member_rows(rows,member):
        if any(r.get('userid')!=member for r in rows):
            raise WeComError('wecom_business_member_scope_changed')

    def _groups(self,member,start,end):
        rows,complete=self._pages('/externalcontact/groupchat/list',
            {'status_filter':0,'owner_filter':{'userid_list':[member]},'limit':50},
            'group_chat_list',lambda r:r.get('chat_id'))
        counts={str(k):0 for k in range(4)}
        for row in rows:
            state=row.get('status')
            if type(state) is not int or state not in range(4):raise WeComError('wecom_business_response_invalid')
            counts[str(state)]+=1
        return {'observed_count':len(rows),'status_counts':counts,'complete':complete,
                'scope':'groups_owned_by_single_visible_member','time_window_applies':False}

    def _messages(self,member,start,end,chat_type):
        rows,complete=self._pages('/externalcontact/get_groupmsg_list_v2',
            {'chat_type':chat_type,'start_time':start,'end_time':end,'filter_type':2,'limit':50},
            'group_msg_list',lambda r:r.get('msgid'))
        # App-visible enterprise records have no creator. Never invent one or
        # filter them out as zero; restrict downstream tasks to the one member.
        tasks=sent_tasks=results=0;statuses={str(k):0 for k in range(4)}
        for row in rows[:MAX_TASKS]:
            if row.get('creator') and row['creator']!=member:raise WeComError('wecom_business_member_scope_changed')
            taskrows,done=self._pages('/externalcontact/get_groupmsg_task',
                {'msgid':identifier(row.get('msgid')),'limit':100},'task_list',lambda r:r.get('userid'))
            self._member_rows(taskrows,member);complete=complete and done
            for task in taskrows:
                status=task.get('status')
                if type(status) is not int or status not in {0,2}:raise WeComError('wecom_business_response_invalid')
                tasks+=1;sent_tasks+=int(status==2)
                sendrows,done=self._pages('/externalcontact/get_groupmsg_send_result',
                    {'msgid':row['msgid'],'userid':member,'limit':100},'send_list',
                    lambda r:r.get('external_userid') if chat_type=='single' else r.get('chat_id'))
                self._member_rows(sendrows,member);complete=complete and done
                for item in sendrows:
                    status=item.get('status')
                    if type(status) is not int or status not in range(4):raise WeComError('wecom_business_response_invalid')
                    statuses[str(status)]+=1;results+=1
        return {'observed_count':len(rows),'tasks_inspected':min(len(rows),MAX_TASKS),
                'member_tasks':tasks,'member_tasks_sent':sent_tasks,'recipient_results_observed':results,
                'recipient_status_counts':statuses,'complete':complete and len(rows)<=MAX_TASKS,
                'recipient_status_legend':{'0':'not_sent','1':'sent','2':'not_friend','3':'other_group_message_limit'},
                'read_count':None,'reply_count':None,'sales_count':None}

    def _moments(self,member,start,end):
        rows,complete=self._pages('/externalcontact/get_moment_list',
            {'start_time':start,'end_time':end,'filter_type':2,'limit':20},'moment_list',lambda r:r.get('moment_id'))
        enterprise=personal=published=pending=0
        for row in rows:
            kind=row.get('create_type')
            if type(kind) is not int or kind not in {0,1}:raise WeComError('wecom_business_response_invalid')
            if kind==1:
                if row.get('creator')!=member:raise WeComError('wecom_business_member_scope_changed')
                personal+=1
            else:enterprise+=1
        inspected=0
        for row in [r for r in rows if r['create_type']==0][:MAX_TASKS]:
            tasks,done=self._pages('/externalcontact/get_moment_task',
                {'moment_id':row['moment_id'],'limit':100},'task_list',lambda r:r.get('userid'))
            self._member_rows(tasks,member);complete=complete and done;inspected+=1
            for item in tasks:
                status=item.get('publish_status')
                if type(status) is not int or status not in {0,1}:raise WeComError('wecom_business_response_invalid')
                published+=int(status==1);pending+=int(status==0)
        return {'observed_count':len(rows),'enterprise_records':enterprise,'personal_records':personal,
                'enterprise_tasks_inspected':inspected,'member_published':published,'member_pending':pending,
                'complete':complete and enterprise<=MAX_TASKS,'deleted_records_included':False,
                'read_count':None,'reply_count':None,'sales_count':None}

    def inspect(self,*,start_time,end_time,acknowledge_business_records=False):
        if acknowledge_business_records is not True:raise WeComError('wecom_business_read_approval_required')
        if (type(start_time) is not int or type(end_time) is not int or not 0<start_time<end_time
                or end_time-start_time>7*86400 or end_time>int(time.time())+300):
            raise WeComError('wecom_business_window_invalid')
        self._requests=0;self._deadline=time.monotonic()+90
        member=self._member()
        output={'protocol':'1.0','window':{'start_time':start_time,'end_time':end_time},
                'visible_member_count':1,'sections':{},'raw_ids_saved':False,'customer_details_read':False,
                'chat_content_read':False,'external_writes':0,'automatic_polling_enabled':False}
        operations={'groups':self._groups,'customer_messages':lambda *a:self._messages(*a,'single'),
                    'group_messages':lambda *a:self._messages(*a,'group'),'moments':self._moments}
        for name,fn in operations.items():
            try:
                result=fn(member,start_time,end_time)
                output['sections'][name]={'status':'ok',**result,'api_read_verified':True,
                    'positive_sample_verified':result['observed_count']>0}
            except WeComError as error:
                output['sections'][name]={'status':'unavailable','observed_count':None,'complete':False,
                    'api_read_verified':False,'positive_sample_verified':False,
                    'error_code':error.code,'api_code':error.api_code}
        output['status']='ok' if all(r['status']=='ok' and r['complete'] for r in output['sections'].values()) else 'partial'
        output['business_post_requests']=self._requests
        return output
