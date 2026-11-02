"""Local drafts and projected receipts only. Approval/submit are Hub-UI-only."""
from .client import REQUEST_KEY, field, require

TASK = field('string', pattern=r'^wecom_moment_[a-f0-9]{32}$')
CATALOG = {
    'wecom.moments-binding': ({}, []),
    'wecom.moments-preflight': ({'request_key': REQUEST_KEY}, ['request_key']),
    'wecom.moments-list': ({}, []),
    'wecom.moments-status': ({'task_id': TASK}, ['task_id']),
    'wecom.moments-receipt': ({'operation': field('string', enum=['draft', 'preflight']),
                              'request_key': REQUEST_KEY}, ['operation', 'request_key']),
    'wecom.moments-draft': ({'request_key': REQUEST_KEY,
        'label': field('string', maxLength=80),
        'binding_id': field('string', pattern=r'^[a-f0-9]{64}$'),
        'text': field('string', maxLength=2000),
        'media_type': field('string', enum=['text', 'images', 'video']),
        'asset_ids': field('array', items=field('string', pattern=r'^asset_[a-f0-9]{32}$'), maxItems=9),
        'customer_tag_ids': field('array', items=field('string', pattern=r'^et[A-Za-z0-9_-]{3,158}$'), minItems=1, maxItems=10),
        'customer_scope_note': field('string', maxLength=500), 'demo_only': field('boolean')},
        ['request_key', 'label', 'binding_id', 'customer_tag_ids', 'customer_scope_note']),
    'wecom.moments-refresh': ({'request_key': REQUEST_KEY, 'task_id': TASK,
        'acknowledge_platform_read': field('boolean')}, ['request_key', 'task_id', 'acknowledge_platform_read']),
}
WRITES = {'wecom.moments-preflight', 'wecom.moments-draft', 'wecom.moments-refresh'}
ROOT = '/api/integrations/wecom/moments'
KEYS = '''id label digest binding_id status phase media_type demo_only task_creation_attempts
 media_upload_attempts creation_job_status creation_scope_rejected member_task_observed member_published
 last_error_code last_api_code last_query_error last_query_api_code created_at updated_at approved_at
 submitted_at last_query_at execution_checked_at media_count customer_tag_count job_known moment_known
 requires_member_confirmation automatic_submit_enabled automatic_polling_enabled external_write_triggered
 platform_refresh_triggered chat_content_read read_count reply_count sales_count evidence_scope replayed
 binding_available binding_fresh write_permission_verified verified_at error_code api_code task_id'''.split()


def projected(raw):
    from .client import pick
    require(type(raw) is dict and raw.get('external_write_triggered') is False,
            'invalid_response', '朋友圈CLI不可审批、创建外部任务或触发外发', 5)
    if 'automatic_submit_enabled' in raw:
        require(raw['automatic_submit_enabled'] is False and raw.get('automatic_polling_enabled') is False,
                'invalid_response', '朋友圈默认不得自动提交或轮询', 5)
    return pick(raw, KEYS)


def call(client, command, args):
    if command == 'wecom.moments-draft':
        text = args.get('text', '')
        kind = args.get('media_type', 'text')
        require(len(text.encode('utf-8')) <= 4000, 'invalid_text_size', '朋友圈正文最多4000个UTF-8字节')
        require(bool(args['label'].strip()) and bool(args['customer_scope_note'].strip()),
                'invalid_arguments', '需填写内容名称和已核对的客户范围说明')
        require(len(set(args['customer_tag_ids'])) == len(args['customer_tag_ids']),
                'invalid_arguments', '企业客户标签不能重复')
        require((kind == 'text' and bool(text.strip()) and not args.get('asset_ids'))
                or (kind != 'text' and bool(args.get('asset_ids'))),
                'invalid_media_selection', '纯文字需有正文且无素材；图片/视频须指定已登记素材')
        raw = client._post(ROOT + '/drafts', {k: v for k, v in args.items() if k != 'request_key'}, args['request_key'])
    elif command == 'wecom.moments-preflight':
        raw = client._post(ROOT + '/preflight', {}, args['request_key'])
    elif command == 'wecom.moments-refresh':
        require(args['acknowledge_platform_read'] is True, 'wecom_read_ack_required',
                '需确认只读查询此任务，不能创建/重发任务', 3)
        raw = client._post(ROOT + '/' + args['task_id'] + '/refresh',
                           {'acknowledge_platform_read': True}, args['request_key'])
    else:
        path = '/binding' if command == 'wecom.moments-binding' else (
            '/' + args['task_id'] if command == 'wecom.moments-status' else (
            '/receipts/' + args['operation'] + '/' + args['request_key'] if command == 'wecom.moments-receipt' else ''))
        raw = client._get(ROOT + path)
    if command == 'wecom.moments-list':
        require(type(raw) is dict and type(raw.get('items')) is list,
                'invalid_response', '朋友圈列表响应不正确', 5)
        return {'items': [projected(row) for row in raw['items']],
                'more_may_exist': raw.get('more_may_exist', False),
                'state_authority': 'existing_creatorhub', 'external_write_triggered': False,
                'platform_refresh_triggered': False}
    return {**projected(raw), 'state_authority': 'existing_creatorhub'}
