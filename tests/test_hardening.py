"""Offline regression tests: real SQLite/services, no external API calls."""
import concurrent.futures
import json
import pathlib
import tempfile
import unittest
from database import db
from services import plan_service as plans, formatting_service as fmt
from services import forward_credit_service as credits
from services.telegram_ownership import claim_session, AccountAlreadyOwned, require_reconnect

class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.previous=db.DB_NAME
        db.DB_NAME=str(pathlib.Path(self.tmp.name)/'test.db')
        db.init_db(); plans.invalidate_plan_configs_cache()
        conn=db.get_connection()
        conn.executemany('INSERT INTO users(telegram_id) VALUES(?)',[(1,),(2,)])
        conn.executemany('INSERT INTO projects(id,user_id,name,status) VALUES(?,?,?,1)',[(10,1,'A'),(11,1,'B'),(20,2,'C')])
        conn.executemany('INSERT INTO destinations(id,project_id,chat_id) VALUES(?,?,?)',[(100,10,'-10001'),(101,10,'-10002'),(200,20,'-20001')])
        conn.commit(); conn.close()
    def tearDown(self):
        db.DB_NAME=self.previous; plans.invalidate_plan_configs_cache(); self.tmp.cleanup()
    def sql(self,sql,args=()):
        conn=db.get_connection()
        try:
            with conn:
                cur=conn.execute(sql,args)
                return [dict(row) for row in cur.fetchall()]
        finally: conn.close()
    def limits(self,value):
        self.sql("UPDATE plan_configs SET daily_forward_limit=?,per_project_daily_forward_limit=? WHERE plan_name='FREE'",(value,value))
        plans.invalidate_plan_configs_cache()

class MigrationTests(DatabaseCase):
    def test_repeat_init_preserves_data(self):
        db.init_db(); db.init_db()
        self.assertEqual(len(self.sql('SELECT * FROM projects')),3)
    def test_foreign_keys_enabled(self):
        self.assertEqual(self.sql('PRAGMA foreign_keys')[0]['foreign_keys'],1)
    def test_defaults(self):
        self.assertEqual(plans.get_plan_limits('PRO')['daily_forward_limit'],1000)
        self.assertEqual(plans.get_plan_limits('CREATOR')['daily_forward_limit'],2000)
    def test_custom_limits_preserved(self):
        self.sql("UPDATE plan_configs SET daily_forward_limit=4500 WHERE plan_name='CREATOR'")
        db.init_db();plans.invalidate_plan_configs_cache()
        self.assertEqual(plans.get_plan_limits('CREATOR')['daily_forward_limit'],4500)
    def test_unidentified_legacy_session_requires_reconnect(self):
        self.sql("INSERT INTO user_telegram_sessions(telegram_id,encrypted_session) VALUES(1,'not-a-real-session')")
        db.init_db()
        self.assertEqual(self.sql('SELECT status FROM user_telegram_sessions')[0]['status'],'reconnect_required')
    def test_migration_from_baseline_schema(self):
        # Real old table shape, not a renamed new table.
        self.sql('DROP TABLE user_telegram_sessions')
        self.sql("CREATE TABLE user_telegram_sessions(telegram_id INTEGER PRIMARY KEY,encrypted_session TEXT NOT NULL,phone_number TEXT,status TEXT DEFAULT 'connected',connected_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.sql("INSERT INTO user_telegram_sessions(telegram_id,encrypted_session) VALUES(1,'legacy-ciphertext')")
        db.init_db(); db.init_db()
        row=self.sql('SELECT * FROM user_telegram_sessions')[0]
        self.assertIsNone(row['external_user_id']);self.assertEqual(row['encrypted_session'],'legacy-ciphertext')

class OwnershipTests(DatabaseCase):
    def test_two_owners_one_external_account(self):
        claim_session(1,900,'cipher-A','phone-A')
        with self.assertRaises(AccountAlreadyOwned):claim_session(2,900,'cipher-B','phone-B')
        self.assertEqual(self.sql('SELECT telegram_id FROM user_telegram_sessions')[0]['telegram_id'],1)
    def test_concurrent_claim(self):
        def work(uid):
            try:claim_session(uid,900,'cipher','phone');return True
            except AccountAlreadyOwned:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(work,[1,2])),1)
    def test_reconnect_retains_claim_until_explicit_disconnect(self):
        claim_session(1,900,'cipher','phone');require_reconnect(1)
        with self.assertRaises(AccountAlreadyOwned):claim_session(2,900,'cipher','phone')
        self.sql('DELETE FROM user_telegram_sessions WHERE telegram_id=1')
        claim_session(2,900,'cipher','phone')
        self.assertEqual(self.sql('SELECT telegram_id FROM user_telegram_sessions')[0]['telegram_id'],2)
    def test_same_owner_update(self):
        claim_session(1,900,'old','phone');claim_session(1,900,'new','phone')
        self.assertEqual(self.sql('SELECT encrypted_session FROM user_telegram_sessions')[0]['encrypted_session'],'new')
    def test_actual_id_required(self):
        for bad in (None,0,-1,'900'):
            with self.assertRaises(ValueError):claim_session(1,bad,'cipher','phone')

class TrialTests(DatabaseCase):
    def test_only_one_trial(self):
        self.assertTrue(plans.start_trial(1));self.assertFalse(plans.start_trial(1))
    def test_expiry_does_not_allow_new_trial(self):
        plans.start_trial(1)
        self.sql("UPDATE users SET plan='FREE',plan_expiry=NULL WHERE telegram_id=1")
        self.assertFalse(plans.start_trial(1))
    def test_concurrent_trial(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(plans.start_trial,[1,1])),1)
    def test_trial_expires_effectively(self):
        plans.start_trial(1)
        self.sql("UPDATE users SET plan_expiry='2000-01-01' WHERE telegram_id=1")
        self.assertEqual(plans.get_user_plan(1),'FREE')

class QuotaTests(DatabaseCase):
    def test_last_unit_race_across_projects(self):
        self.limits(1)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result=list(pool.map(lambda pid:credits.reserve(1,pid,str(pid)),[10,11]))
        self.assertEqual(sum(bool(x) for x in result),1)
    def test_credits_after_daily_allowance(self):
        self.limits(1); credits.adjust(1,2,'grant','test',99)
        ref=credits.reserve(1,10,'a');credits.finish(ref,True)
        self.assertEqual(credits.balance(1),2)
        ref=credits.reserve(1,11,'b');credits.finish(ref,True)
        self.assertEqual(credits.balance(1),1)
    def test_failure_refunds_credit_once(self):
        self.limits(0);credits.adjust(1,1,'grant','test')
        ref=credits.reserve(1,10,'a');self.assertEqual(credits.balance(1),0)
        self.assertTrue(credits.finish(ref,False));self.assertFalse(credits.finish(ref,False))
        self.assertEqual(credits.balance(1),1)
    def test_failure_refunds_daily_once(self):
        self.limits(1);ref=credits.reserve(1,10,'a');credits.finish(ref,False);credits.finish(ref,False)
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],0)
    def test_idempotent_credit_grant(self):
        self.assertTrue(credits.adjust(1,10,'grant','test',99))
        self.assertFalse(credits.adjust(1,10,'grant','test',99))
        self.assertEqual(credits.balance(1),10)
    def test_reference_reuse_rejected(self):
        credits.adjust(1,10,'grant','test')
        with self.assertRaises(ValueError):credits.adjust(2,10,'grant','test')
    def test_committed_reference_does_not_double_consume(self):
        ref=credits.reserve(1,10,'same');credits.finish(ref,True)
        self.assertEqual(credits.reserve(1,10,'same'),ref)
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],1)
    def test_pending_reference_not_replayed(self):
        credits.reserve(1,10,'same');self.assertIsNone(credits.reserve(1,10,'same'))
    def test_cross_user_denied(self):
        with self.assertRaises(PermissionError):credits.reserve(2,10,'forged')
    def test_suspended_user_denied(self):
        self.sql("UPDATE users SET status='suspended' WHERE telegram_id=1")
        with self.assertRaises(PermissionError):credits.reserve(1,10,'forged')
    def test_delete_does_not_reset_allowance(self):
        self.limits(1);ref=credits.reserve(1,10,'a');credits.finish(ref,True)
        self.sql('DELETE FROM projects WHERE id=10')
        self.assertIsNone(credits.reserve(1,11,'b'))
    def test_midnight_refund_uses_original_day(self):
        credits.reserve(1,10,'a')
        self.sql("UPDATE daily_usage SET usage_date='2000-01-01'")
        self.sql("UPDATE forward_reservations SET usage_date='2000-01-01'")
        credits.finish('a',False)
        self.assertEqual(self.sql('SELECT forward_count FROM daily_usage')[0]['forward_count'],0)
    def test_no_negative_credits(self):
        with self.assertRaises(ValueError):credits.adjust(1,-1,'revoke','test')
    def test_credits_race_last_unit(self):
        self.limits(0);credits.adjust(1,1,'g','test')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda p:credits.reserve(1,p,str(p)),[10,11]))
        self.assertEqual(sum(bool(r) for r in results),1);self.assertEqual(credits.balance(1),0)

class FormattingTests(DatabaseCase):
    def test_default_exact_noop(self):
        text='Hello  world\n\nhttps://example.com?q=9'
        self.assertEqual(fmt.apply_formatting(10,text),text)
    def test_remove_handles_not_emails(self):
        fmt.configure(1,10,{'remove_usernames':True})
        self.assertEqual(fmt.preview(1,10,'Hi @example email me@example.com'),'Hi email me@example.com')
    def test_remove_links_not_emails(self):
        fmt.configure(1,10,{'remove_links':True})
        self.assertEqual(fmt.preview(1,10,'me@sub.example.com https://example.com t.me/hello www.foo.com foo.org'),'me@sub.example.com')
    def test_destination_override(self):
        fmt.configure(1,10,{'header':'Default','footer':'Footer'})
        fmt.configure(1,10,{'header':'Target'},100)
        self.assertEqual(fmt.preview(1,10,'Body',100),'Target\n\nBody\n\nFooter')
        self.assertEqual(fmt.preview(1,10,'Body',101),'Default\n\nBody\n\nFooter')
    def test_destination_cross_project_rejected(self):
        with self.assertRaises(PermissionError):fmt.configure(1,10,{'header':'no'},200)
    def test_project_cross_user_rejected(self):
        with self.assertRaises(PermissionError):fmt.preview(2,10,'private')
        with self.assertRaises(PermissionError):fmt.configure(2,10,{'mono':True})
    def test_invalid_options_rejected(self):
        for cfg in ({'mono':1},{'remove_first_lines':-1},{'unknown':True},{'keep_first_words':'2'}):
            with self.assertRaises(ValueError):fmt.configure(1,10,cfg)
    def test_trim_lines_then_words(self):
        fmt.configure(1,10,{'remove_first_lines':1,'remove_last_lines':1,'keep_first_words':2})
        self.assertEqual(fmt.preview(1,10,'intro\none two three\nend'),'one two')
    def test_trim_empty_and_short(self):
        fmt.configure(1,10,{'remove_last_words':20})
        self.assertEqual(fmt.preview(1,10,'one'),'');self.assertEqual(fmt.preview(1,10,''),'')
    def test_keep_zero_empty(self):
        fmt.configure(1,10,{'keep_first_lines':0})
        self.assertEqual(fmt.preview(1,10,'one\ntwo'),'')
    def test_url_placeholder_corruption_regression(self):
        fmt.add_replace_rule(10,'URL0','bad');fmt.add_replace_rule(10,'9','')
        self.assertEqual(fmt.preview(1,10,'9 https://example.com/9'),' https://example.com/9')
    def test_invalid_regex_rejected_before_write(self):
        with self.assertRaises(Exception):fmt.add_remove_pattern(10,'[')
        self.assertEqual(fmt.get_remove_patterns(10),[])
    def test_old_invalid_regex_no_crash(self):
        fmt.get_rules(10);self.sql('UPDATE formatting_rules SET remove_patterns=? WHERE project_id=10',(json.dumps(['[']),))
        self.assertEqual(fmt.preview(1,10,'hello'),'hello')
    def test_header_footer_and_legacy_prefix(self):
        fmt.set_prefix(10,'Prefix');fmt.configure(1,10,{'footer':'Footer'})
        self.assertEqual(fmt.preview(1,10,'Body'),'Prefix\n\nBody\n\nFooter')
    def test_settings_persist(self):
        fmt.configure(1,10,{'link_preview':False,'disable_hidden_links':True,'mono':True})
        db.init_db()
        cfg=fmt.get_advanced(10);self.assertFalse(cfg['link_preview']);self.assertTrue(cfg['mono'])

if __name__=='__main__':unittest.main()
