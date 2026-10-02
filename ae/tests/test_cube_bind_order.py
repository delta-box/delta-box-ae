"""Docker recreation order regression; service and Docker calls are mocked."""
import copy
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from ae.scripts import cube_control_context as m

candidate=os.environ.get('CUBE_BIND_CANDIDATE')
if candidate:
    spec=importlib.util.spec_from_file_location('cube_bind_candidate',candidate)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

BINDS=['data-volume:/var/lib/mysql:rw','/sql:/docker-entrypoint-initdb.d:ro']

def identity(binds, **host_extra):
    data={'Id':'a'*64,'Config':{'Hostname':'a'*12,'Env':['B=two','A=one']},
          'HostConfig':{'CpusetCpus':'','CpusetMems':'','Binds':binds,**host_extra},
          'State':{'Running':False,'Pid':0,'StartedAt':'x','FinishedAt':'y'},
          'Mounts':[{'Type':'volume','Source':'data','Destination':'/var/lib/mysql','RW':True}],
          'Image':'sha256:unchanged'}
    with patch.object(m,'output',return_value=json.dumps([data])):result=m.inspect('fake')
    result.update(running=True,effective_cpus='0-95',effective_mems='0-5')
    return result

class Tests(unittest.TestCase):
    def test_recreated_disjoint_binds_pass_and_log(self):
        before=identity(BINDS);after=identity(BINDS[::-1]);log=io.StringIO()
        with patch.object(m,'inspect',return_value=after),redirect_stdout(log):
            self.assertEqual(m.verify_mysql_container(before,same_id=False),after)
        self.assertIn('mysql-independent-bind-order',log.getvalue())
        self.assertEqual(after['host_binds'],BINDS[::-1])

    def test_real_source_destination_mode_and_multiplicity_changes_fail(self):
        before=identity(BINDS)
        for changed in ([BINDS[0].replace('data-volume','wrong-volume'),BINDS[1]],
                        [BINDS[0].replace('/var/lib/mysql','/var/lib/else'),BINDS[1]],
                        [BINDS[0].replace(':rw',':ro'),BINDS[1]], BINDS+[BINDS[0]], BINDS[:1]):
            with self.subTest(changed=changed),patch.object(m,'inspect',return_value=identity(changed)):
                with self.assertRaises(RuntimeError):m.verify_mysql_container(before,same_id=False)

    def test_ambiguous_and_overlapping_orders_remain_distinct(self):
        for binds in (['a:/same:rw','b:/same:rw'],['a:/parent:rw','b:/parent/child:ro'],
                      ['a:/var/../data:rw','b:/x:ro'],['a:/var//data:rw','b:/x:ro'],
                      ['a:/data/:rw','b:/x:ro'],['a:relative:rw','b:/x:ro'],
                      ['a:/:rw','b:/x:ro'],['a:/data','b:/x:ro'],['bad','b:/x:ro']):
            with self.subTest(binds=binds):
                self.assertNotEqual(identity(binds)['config_sha256'],identity(binds[::-1])['config_sha256'])

    def test_other_ordered_host_configuration_still_fails(self):
        before=identity(BINDS,Dns=['1.1.1.1','8.8.8.8'])
        after=identity(BINDS[::-1],Dns=['8.8.8.8','1.1.1.1'])
        with patch.object(m,'inspect',return_value=after):
            with self.assertRaises(RuntimeError):m.verify_mysql_container(before,same_id=False)

    def test_mount_image_placement_and_required_id_still_fail(self):
        before=identity(BINDS)
        for key,value in [('image_id','different'),('effective_cpus','4-7'),('effective_mems','3'),
                          ('mounts',[]),('running',False),('id','new-id')]:
            after=identity(BINDS[::-1]);after[key]=value
            with self.subTest(key=key),patch.object(m,'inspect',return_value=after):
                with self.assertRaises(RuntimeError):m.verify_mysql_container(before,same_id=True)

if __name__=='__main__':unittest.main()
