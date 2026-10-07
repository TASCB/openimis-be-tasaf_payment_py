from django.db import migrations


def _set_summary(ApprovalRequest, Task, requests, summary):
    requests.update(summary=summary)
    request_ids = [str(r) for r in requests.values_list('id', flat=True)]
    for task in Task.objects.filter(data__approval_request_id__in=request_ids):
        Task.objects.filter(id=task.id).update(data={**(task.data or {}), 'summary': summary})


def rewrite_summaries(apps, schema_editor):
    """Readable summaries for MUSE changes and paylists raised before they existed, on the
    approval request and on the copy its inbox tasks carry."""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ApprovalRequest = apps.get_model('approval', 'ApprovalRequest')
    Task = apps.get_model('tasks_management', 'Task')

    change_type = ContentType.objects.filter(app_label='tasaf_payment', model='musechangerequest').first()
    if change_type and ApprovalRequest.objects.filter(content_type=change_type).exists():
        from tasaf_payment.muse_setup import describe_change
        MuseChangeRequest = apps.get_model('tasaf_payment', 'MuseChangeRequest')
        for change in MuseChangeRequest.objects.all():
            summary = describe_change(change.kind, change.fsp_code, change.current, change.proposed or {},
                                      (change.json_ext or {}).get('reason') or '')
            _set_summary(ApprovalRequest, Task, ApprovalRequest.objects.filter(
                content_type=change_type, object_id=str(change.id)), summary)

    paylist_type = ContentType.objects.filter(app_label='tasaf_payment', model='paylist').first()
    if paylist_type and ApprovalRequest.objects.filter(content_type=paylist_type).exists():
        # Paylist totals need the live relations (payroll, cycle, items, programme).
        from tasaf_payment.models import Paylist
        from tasaf_payment.services import describe_paylist
        for request in ApprovalRequest.objects.filter(content_type=paylist_type):
            paylist = Paylist.objects.filter(id=request.object_id).first()
            if paylist:
                _set_summary(ApprovalRequest, Task, ApprovalRequest.objects.filter(id=request.id),
                             describe_paylist(paylist))


class Migration(migrations.Migration):

    dependencies = [
        ('tasaf_payment', '0024_rejected_at_approval_status'),
        ('approval', '0004_approvalstep_assigned_group_id'),
        ('tasks_management', '0004_auto_20230628_1404'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(rewrite_summaries, migrations.RunPython.noop),
    ]
