"""Dependency-free isolated execution of the actual authorization/referral helper code.
Does not connect to the production database or exercise HTTP routes.
"""
import importlib.util, sys, types
from pathlib import Path
from types import SimpleNamespace as NS
ROOT=Path(__file__).resolve().parents[1]
results=[]
def check(label,fn):
 try:fn();results.append(('PASS',label))
 except Exception as e:results.append(('FAIL',label+' '+repr(e)))
def expect(value,expected):
 assert value==expected,(value,expected)

# Inject narrow stand-ins for external framework imports; execute actual decorators.
flask=types.ModuleType('flask')
class Forbidden(Exception):pass
flask.abort=lambda code: (_ for _ in ()).throw(Forbidden(code))
flask.redirect=lambda path: ('REDIRECT',path)
flask.url_for=lambda name:'/login' if name=='auth.login' else name
sys.modules['flask']=flask
fl=types.ModuleType('flask_login');fl.current_user=NS(is_authenticated=False,role=None,admin=None)
sys.modules['flask_login']=fl
spec=importlib.util.spec_from_file_location('isolated_decorators',ROOT/'website/utils/decorators.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
role=mod.role_required('admin')(lambda:'OK')
super_role=mod.super_admin_required(lambda:'OK')
def denied(call):
 try:call()
 except Forbidden as e:expect(e.args[0],403);return
 raise AssertionError('Expected 403')
check('Anonymous role guard redirects',lambda:expect(role(),('REDIRECT','/login')))
check('Anonymous super-admin guard redirects',lambda:expect(super_role(),('REDIRECT','/login')))
fl.current_user.__dict__.update(is_authenticated=True,role='employer',admin=None)
check('Employer blocked from admin',lambda:denied(role))
check('Employer blocked from super-admin',lambda:denied(super_role))
fl.current_user.__dict__.update(is_authenticated=True,role='jobseeker',admin=None)
check('Jobseeker blocked from admin',lambda:denied(role))
fl.current_user.__dict__.update(is_authenticated=True,role='admin',admin=NS(is_super_admin=False))
check('Regular admin allowed admin route',lambda:expect(role(),'OK'))
check('Regular admin blocked super-admin route',lambda:denied(super_role))
fl.current_user.__dict__.update(is_authenticated=True,role='admin',admin=NS(is_super_admin=True))
check('Super admin allowed super-admin route',lambda:expect(super_role(),'OK'))

# Execute actual referral validation helpers with lightweight model/database stubs.
website=types.ModuleType('website');website.__path__=[];sys.modules['website']=website
ext=types.ModuleType('website.extensions');ext.db=object();sys.modules['website.extensions']=ext
models=types.ModuleType('website.models')
class FakeModel:pass
models.Application=models.Job=models.Referral=FakeModel
sys.modules['website.models']=models
spec=importlib.util.spec_from_file_location('isolated_referral',ROOT/'website/utils/referral_flow.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
check('Missing referral job rejected',lambda:expect(r.validate_referral_job(None)[0],False))
check('Unverified jobseeker rejected',lambda:expect(r.validate_jobseeker_for_referral(NS(is_verified=False))[0],False))
check('Verified jobseeker accepted',lambda:expect(r.validate_jobseeker_for_referral(NS(is_verified=True))[0],True))
check('Missing application cannot issue referral',lambda:expect(r.validate_referral_for_issue(None)[0],False))
check('Missing application cannot reject referral',lambda:expect(r.validate_referral_for_rejection(None)[0],False))
check('Referral on non-referral job rejected',lambda:expect(r.validate_referral_job(NS(job_type='Job Fair'))[0],False))
check('Unapproved referral job rejected',lambda:expect(r.validate_referral_job(NS(job_type='Referral',approval_status='Pending'))[0],False))
check('Closed referral job rejected',lambda:expect(r.validate_referral_job(NS(job_type='Referral',approval_status='Approved',status='Closed'))[0],False))
from datetime import date,timedelta
check('Past deadline referral rejected',lambda:expect(r.validate_referral_job(NS(job_type='Referral',approval_status='Approved',status='Open',deadline=date.today()-timedelta(days=1)))[0],False))
for status,label in results:print(f'{status}: {label}')
print(f'TOTAL: {sum(s=="PASS" for s,_ in results)}/{len(results)}')
if any(s!='PASS' for s,_ in results):sys.exit(1)
