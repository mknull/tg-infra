"""Behavioral installer checks: real wizard/configuration/verification and service adapter."""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.setup_harness import InstallHarness
from lib.features import selected_services


class TestSetupScript(unittest.TestCase):
    def setUp(self):
        self.install=InstallHarness()
        self.addCleanup(self.install.close)

    def assert_no_activation(self, install):
        self.assertFalse((install.data/'state/.env').exists())
        if install.systemd_log.exists():
            state=json.loads(install.systemd_log.read_text())
            self.assertEqual(state['active'],[])
            self.assertEqual(state['enabled'],[])
            self.assertTrue(all(c[0]=='show-environment' for c in state['calls']))

    def test_save_then_resume_and_start(self):
        code, text=self.install.drive(self.install.fresh_answers())
        self.assertEqual(code,0,text)
        self.assertIn('Proposal saved privately',text)
        self.assert_no_activation(self.install)
        self.assertLess(text.index('This installer currently uses Telegram'),text.index('RSS/Atom job feeds?'))
        self.assertLess(text.index('These calls are billed'),text.index('Generate this proposal now?'))
        self.assertLess(text.index('Processing existing entries uses paid matching'),text.index('[R]ecent 30 days'))
        self.assertIn('no application-wide spending cap',text)
        self.assertNotIn('fixture-model-key',text)
        self.assertNotIn('123:fixture',text)
        calls=sum(c['kind']=='model' for c in self.install.calls())
        code,text=self.install.drive([('A saved proposal','r'),('[S]tart / [C]ustomize','s'),("Press Enter once you've sent it",'')])
        self.assertEqual(code,0,text)
        self.assertIn('Input access and Telegram delivery verified',text)
        self.assertEqual(calls,sum(c['kind']=='model' for c in self.install.calls()))
        state=self.install.data/'state'
        self.assertTrue((state/'.env').exists())
        self.assertFalse((state/'email-triage-criteria.md').exists())
        self.assertFalse((state/'it-jobs-session.session').exists())
        manifest=json.loads((state/'installation.json').read_text())
        systemd=json.loads(self.install.systemd_log.read_text())
        self.assertEqual(set(systemd['active']),{n+'.timer' for n in selected_services(manifest)})
        self.assertEqual((state/'.env').stat().st_mode & 0o777,0o600)
        self.assertFalse((state/'setup/pending.json').exists())
        self.assertFalse(any(c['kind'] in ('telegram_auth','graph','outlook_auth') for c in self.install.calls()))

    def test_all_seven_input_combinations_activate_only_required_services(self):
        for rss,telegram,email in itertools.product((False,True), repeat=3):
            if not any((rss,telegram,email)): continue
            with self.subTest(rss=rss,telegram=telegram,email=email):
                install=InstallHarness()
                try:
                    code,text=install.drive(install.fresh_answers(rss=rss,telegram=telegram,email=email,action='s'))
                    self.assertEqual(code,0,text)
                    state=install.data/'state'
                    manifest=json.loads((state/'installation.json').read_text())
                    systemd=json.loads(install.systemd_log.read_text())
                    self.assertEqual(set(systemd['active']),{n+'.timer' for n in selected_services(manifest)})
                    kinds={c['kind'] for c in install.calls()}
                    self.assertEqual('telegram_auth' in kinds,telegram)
                    self.assertEqual('outlook_auth' in kinds,email)
                    self.assertEqual('feed' in kinds,rss)
                    self.assertEqual((state/'email-triage-criteria.md').exists(),email)
                    self.assertEqual((state/'it-jobs-criteria.md').exists(),rss or telegram)
                finally: install.close()

    def test_reinstall_keep_no_model_calls_or_unwanted_services(self):
        code,text=self.install.drive(self.install.fresh_answers(action='s'))
        self.assertEqual(code,0,text)
        state=self.install.data/'state'
        before={p.name:p.read_bytes() for p in state.glob('*.json') if p.name!='setup-verification.json'}
        calls=sum(c['kind']=='model' for c in self.install.calls())
        code,text=self.install.drive([('[K]eep existing / [C]hange','k'),('[S]tart / [C]ustomize','s')])
        self.assertEqual(code,0,text)
        self.assertEqual(calls,sum(c['kind']=='model' for c in self.install.calls()))
        self.assertTrue(all((state/name).read_bytes()==data for name,data in before.items()))
        self.assertFalse(any('weekly' in u for u in json.loads(self.install.systemd_log.read_text())['active']))

    def test_cancel_changes_no_active_configuration(self):
        answers=self.install.fresh_answers(action='q')
        code,text=self.install.drive(answers)
        self.assertEqual(code,1,text)
        self.assert_no_activation(self.install)

    def test_missing_prerequisites_block_generation_and_downloads_but_allow_save(self):
        for missing in ('systemd','docker','pdf'):
            with self.subTest(missing=missing):
                install=InstallHarness()
                try:
                    marker={'systemd':str(install.systemd_log)+'.unavailable',
                            'docker':str(install.docker_log)+'.fail',
                            'pdf':str(install.provider_log)+'.pdf-missing'}[missing]
                    Path(marker).touch()
                    answers=install.fresh_answers(briefings=missing!='systemd')
                    answers=answers[:next(i for i,(prompt,_) in enumerate(answers) if prompt.startswith('Generate job-matching'))]
                    code,text=install.drive(answers+[('[S]tart / [C]ustomize','l')])
                    self.assertEqual(code,0,text)
                    self.assertIn('Prerequisites need attention before paid generation',text)
                    self.assertIn({'systemd':'working user systemd manager','docker':'docker ps','pdf':'libpango'}[missing],text)
                    self.assertNotIn('Generate job-matching instructions',text)
                    self.assertEqual(install.calls(),[])
                    self.assertFalse(install.download_log.exists())
                    self.assert_no_activation(install)
                    self.assertTrue((install.data/'state/setup/pending.json').exists())
                finally:
                    install.close()

    def test_disabling_briefings_recovers_from_missing_docker(self):
        Path(str(self.install.docker_log)+'.fail').touch()
        answers=self.install.fresh_answers(briefings=True)
        answers=answers[:next(i for i,(prompt,_) in enumerate(answers) if prompt.startswith('Generate job-matching'))]
        answers += [('[S]tart / [C]ustomize','c'),('Customize [F]eatures','f'),
                    ('RSS/Atom job feeds?','y'),('Telegram groups as an INPUT','n'),('Outlook job-email input?','n'),
                    ('Researched /briefme reports','n'),('Weekly reports and email feedback','n'),('Hourly Telegram delivery','n'),
                    ('TELEGRAM_BOT_TOKEN [set',''),('DEEPSEEK_API_KEY [set',''),('USER_NAME [set',''),
                    ('[S]tart / [C]ustomize','c'),('Customize [F]eatures','m')]
        matching=self.install.fresh_answers(action='s')
        answers += matching[next(i for i,(prompt,_) in enumerate(matching) if prompt.startswith('Generate job-matching')):]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertLess(text.index('No generation started'),text.index('Generate job-matching instructions'))
        self.assertEqual(self.install.docker_log.read_text().splitlines(),['["ps"]'])
        self.assertFalse(self.install.download_log.exists())
        self.assertFalse(json.loads((self.install.data/'state/installation.json').read_text())['features']['briefings'])

    def test_custom_search_recovers_without_local_docker(self):
        Path(str(self.install.docker_log)+'.fail').touch()
        editor=self.install.commands/'edit-search'
        editor.write_text('#!'+sys.executable+' -S\nimport sys\nfrom pathlib import Path\np=Path(sys.argv[1])\np.write_text(p.read_text().replace("http://localhost:8080/search", "https://search.example.test/search"))\n')
        editor.chmod(0o755)
        self.install.env['VISUAL']=str(editor)
        answers=self.install.fresh_answers(briefings=True)
        answers=answers[:next(i for i,(prompt,_) in enumerate(answers) if prompt.startswith('Generate job-matching'))]
        answers += [('[S]tart / [C]ustomize','c'),('Customize [F]eatures','e'),('File number','1'),
                    ('[S]tart / [C]ustomize','c'),('Customize [F]eatures','m')]
        matching=self.install.fresh_answers(briefings=True,action='s')
        answers += matching[next(i for i,(prompt,_) in enumerate(matching) if prompt.startswith('Generate job-matching')):]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertIn('no local search container will be created',text)
        self.assertIn('Briefing browser, search and PDF verified',text)
        self.assertEqual(self.install.docker_log.read_text().splitlines(),['["ps"]'])
        self.assertIn('install chromium',self.install.download_log.read_text())

    def test_rss_only_needs_no_docker_or_pdf_runtime(self):
        Path(str(self.install.docker_log)+'.fail').touch()
        Path(str(self.install.provider_log)+'.pdf-missing').touch()
        code,text=self.install.drive(self.install.fresh_answers(action='s'))
        self.assertEqual(code,0,text)
        self.assertFalse(self.install.docker_log.exists())
        self.assertFalse(self.install.download_log.exists())
        self.assertFalse(any(c['kind'] in ('browser','search','graph','telegram_auth') for c in self.install.calls()))

    def test_unreadable_pdf_explains_text_alternative_before_paid_calls(self):
        docs=self.install.path/'documents'
        docs.mkdir()
        (docs/'cv.pdf').write_bytes(b'%PDF-fixture')
        command=self.install.commands/'pdftotext'
        command.write_text('#!/bin/sh\nexit 127\n')
        command.chmod(0o755)
        answers=self.install.fresh_answers()
        answers=answers[:next(i for i,(prompt,_) in enumerate(answers) if prompt.startswith('Documents directory'))]
        answers += [('Documents directory',str(docs)),('[S]tart / [C]ustomize','l')]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertLess(text.index('PDFs need pdftotext'),text.index('Documents directory'))
        self.assertIn('supply TXT/MD notes instead',text)
        self.assertEqual(self.install.calls(),[])
        self.assert_no_activation(self.install)

    def test_failed_activation_restores_files_and_service_state(self):
        self.install.systemd_log.with_suffix('.json.fail').touch()
        answers=self.install.fresh_answers(action='s') + [('[S]tart / [C]ustomize','l')]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertIn('fixture activation failure',text)
        self.assertFalse((self.install.data/'state/installation.json').exists())
        self.assertEqual(json.loads(self.install.systemd_log.read_text())['active'],[])
        self.assertEqual(json.loads(self.install.systemd_log.read_text())['enabled'],[])

    def test_delivery_failure_never_activates(self):
        Path(str(self.install.provider_log)+'.delivery-fail').touch()
        answers=self.install.fresh_answers(action='s')+[('[S]tart / [C]ustomize','l')]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertIn('CANARY FAILED',text)
        self.assertFalse((self.install.data/'state/.env').exists())
        calls=json.loads(self.install.systemd_log.read_text())['calls']
        self.assertFalse(any(c[0] in ('enable','start','stop','disable') for c in calls))

    def test_stale_saved_proposal_does_not_overwrite_active_changes(self):
        code,text=self.install.drive(self.install.fresh_answers())
        self.assertEqual(code,0,text)
        path=self.install.data/'state/.env'
        path.write_text('USER_NAME=Changed while proposal was saved\n')
        code,text=self.install.drive([('A saved proposal','r')])
        self.assertEqual(code,1,text)
        self.assertIn('Active settings changed',text)
        self.assertEqual(path.read_text(),'USER_NAME=Changed while proposal was saved\n')

    def test_weekly_reports_independent_of_email_input(self):
        code,text=self.install.drive(self.install.fresh_answers(weekly=True,action='s'))
        self.assertEqual(code,0,text)
        units=json.loads(self.install.systemd_log.read_text())['active']
        self.assertIn('weekly-trend.timer',units)
        self.assertIn('feedback-poller.timer',units)
        self.assertNotIn('email-ingest.timer',units)
        paths=[c['path'] for c in self.install.calls() if c['kind']=='graph']
        self.assertEqual(paths,['me/mailFolders/sentitems/messages'])

    def test_briefings_and_canary_are_optional_and_verified(self):
        code,text=self.install.drive(self.install.fresh_answers(briefings=True,canary=True,action='s'))
        self.assertEqual(code,0,text)
        self.assertIn('Briefing browser, search and PDF verified',text)
        self.assertTrue((self.install.data/'skills/job-brief/references/profile-for-relevance.md').exists())
        self.assertIn('delivery-canary.timer',json.loads(self.install.systemd_log.read_text())['active'])
        kinds={c['kind'] for c in self.install.calls()}
        self.assertTrue({'browser','search'} <= kinds)

    def test_invalid_flash_json_is_reported_and_never_activates(self):
        Path(str(self.install.provider_log)+'.invalid-flash').touch()
        answers=self.install.fresh_answers()
        answers=answers[:next(i for i,(prompt,_) in enumerate(answers) if prompt.startswith('[A]pply'))]
        answers += [('[R]etry now / Finish [L]ater','l'),('[S]tart / [C]ustomize','l')]
        code,text=self.install.drive(answers)
        self.assertEqual(code,0,text)
        self.assertIn('invalid JSON',text)
        self.assertIn('Still needed:',text)
        self.assertFalse((self.install.data/'source/interests.txt').exists())
        self.assert_no_activation(self.install)
        from lib.setup_session import pending
        self.assertIn('invalid JSON',(pending(self.install.data)/'state/setup.log').read_text())

    def test_reinstall_disable_preserves_history_then_reenable(self):
        code,text=self.install.drive(self.install.fresh_answers(telegram=True,action='s'))
        self.assertEqual(code,0,text)
        state=self.install.data/'state'
        (state/'message_queue').mkdir(exist_ok=True)
        queue=state/'message_queue/retained.json'
        queue.write_text('{"content":"queued job"}')
        history=state/'history-fixture.json'
        history.write_text('{"already_delivered":42}')
        for telegram in (False,True):
            calls=sum(c['kind']=='model' for c in self.install.calls())
            answers=[('[K]eep existing / [C]hange','c'),('RSS/Atom job feeds?', 'y'),
                     ('Telegram groups as an INPUT','y' if telegram else 'n'),('Outlook job-email input?','n'),
                     ('Researched /briefme reports','n'),('Weekly reports and email feedback','n'),('Hourly Telegram delivery','n'),
                     ('TELEGRAM_BOT_TOKEN [set',''),('DEEPSEEK_API_KEY [set','')]
            if telegram: answers += [('TELEGRAM_API_ID [set',''),('TELEGRAM_API_HASH [set','')]
            answers += [('USER_NAME [set','')]
            if telegram: answers += [('[K]eep / [E]dit the source list','k')]
            answers += [('[S]tart / [C]ustomize','s')]
            code,text=self.install.drive(answers)
            self.assertEqual(code,0,text)
            self.assertEqual(calls,sum(c['kind']=='model' for c in self.install.calls()))
            self.assertEqual('telegram-poll.timer' in json.loads(self.install.systemd_log.read_text())['active'],telegram)
            self.assertEqual(queue.read_text(),'{"content":"queued job"}')
            self.assertEqual(history.read_text(),'{"already_delivered":42}')

    def test_reinstall_verification_failure_restores_running_services(self):
        code,text=self.install.drive(self.install.fresh_answers(action='s'))
        self.assertEqual(code,0,text)
        before=json.loads(self.install.systemd_log.read_text())
        config=(self.install.data/'state/.env').read_bytes()
        Path(str(self.install.provider_log)+'.feed-fail').touch()
        code,text=self.install.drive([('[K]eep existing / [C]hange','k'),('[S]tart / [C]ustomize','s'),('[S]tart / [C]ustomize','l')])
        self.assertEqual(code,0,text)
        after=json.loads(self.install.systemd_log.read_text())
        self.assertEqual(set(after['active']),set(before['active']))
        self.assertEqual(set(after['enabled']),set(before['enabled']))
        self.assertEqual((self.install.data/'state/.env').read_bytes(),config)

    def test_disabled_workers_make_no_provider_requests(self):
        from lib.features import DEFAULTS
        state=self.install.data/'state'
        state.mkdir()
        for script,feature in [('rss_poller.py','rss'),('it_jobs_poller.py','telegram'),
                               ('email_triage.py','email'),('weekly_trend.py','weekly'),
                               ('weekly_recovery.py','weekly'),('feedback_poller.py','weekly'),
                               ('delivery_canary.py','canary'),('it_jobs_triage.py','rss')]:
            with self.subTest(script=script):
                features={**DEFAULTS,feature:False,'email':feature=='rss'}
                (state/'installation.json').write_text(json.dumps({'version':1,'features':features,'schedules':{}}))
                result=subprocess.run([sys.executable,str(self.install.code/script)],cwd=self.install.code,
                                      env=self.install.env,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.install.calls(),[])


class AuthenticationState(unittest.TestCase):
    def test_refreshed_token_is_retained_when_configuration_rolls_back(self):
        from lib import setup_session as session
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'state').mkdir()
            (root/'state/.env').write_text('OUTLOOK_CLIENT_ID=app\nOUTLOOK_EMAIL=alex@example.test\n')
            token=root/'state/outlook-token.json'
            token.write_text('{"refresh_token":"original"}')
            proposal=session.create(root)
            # A worker refreshes between proposal creation and Start.
            token.write_text('{"refresh_token":"worker"}')
            session.sync_auth(root,proposal)
            copied=proposal/'state/outlook-token.json'
            self.assertEqual(json.loads(copied.read_text())['refresh_token'],'worker')
            copied.write_text('{"refresh_token":"verification"}')
            session.write_env(proposal/'state/.env',{'USER_NAME':'Alex'})
            backup=session.apply(root,proposal)
            session.restore(backup)
            session.sync_auth(root,proposal,to_active=True)
            self.assertEqual(json.loads(token.read_text())['refresh_token'],'verification')
            self.assertNotIn('USER_NAME',(root/'state/.env').read_text())

if __name__=='__main__': unittest.main()
