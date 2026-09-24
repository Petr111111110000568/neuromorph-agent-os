import math
import unittest
from workbench.plugins import execute,validate
class PluginTests(unittest.TestCase):
    def test_zero_perturbation_is_exact_baseline(self):
        result=execute('coupled_dynamics',{'perturbation':0})
        self.assertEqual(result['metrics'][0]['value'],0)
        self.assertEqual(result['series'][0]['points'],result['series'][1]['points'])
    def test_quantum_zero_angle(self):
        r=execute('quantum_circuit',{'theta':0,'shots':100})
        self.assertEqual(r['table'][0]['counts'],100)
        self.assertEqual(sum(x['counts'] for x in r['table'][1:]),0)
    def test_diffusion_conserves_mean_but_changes_local_response(self):
        a=execute('coupled_dynamics',{'coupling':0})
        b=execute('coupled_dynamics',{'coupling':1})
        for x,y in zip(a['series'][1]['points'],b['series'][1]['points']):
            self.assertAlmostEqual(x['y'],y['y'],places=12)
        self.assertNotEqual(a['series'][3]['points'],b['series'][3]['points'])
    def test_quantum_bell_and_depolarizing(self):
        r=execute('quantum_circuit',{'theta':math.pi/2,'shots':10000})
        self.assertAlmostEqual(r['table'][0]['theory_probability'],.5)
        self.assertEqual(r['table'][1]['counts'],0)
        r=execute('quantum_circuit',{'depolarizing':1})
        self.assertEqual([x['theory_probability'] for x in r['table']],[.25]*4)
    def test_reproducibility(self):
        for plugin in ['coupled_dynamics','regression_benchmark','quantum_circuit']:
            self.assertEqual(execute(plugin,{'seed':12}),execute(plugin,{'seed':12}))
    def test_holdout_signal(self):
        r=execute('regression_benchmark',{'noise':0,'seed':42})
        self.assertLess(r['table'][1]['test_mse'],r['table'][0]['test_mse']/5)
        self.assertFalse(r['validation']['test_used_for_fitting'])
    def test_nonfinite_boolean_and_unknown_rejected(self):
        for parameters in [{'shots':True},{'theta':float('nan')},{'seed':-1},{'unknown':0}]:
            with self.assertRaises(ValueError):validate('quantum_circuit',parameters)
    def test_contract_self_dependency(self):
        r=execute('legacy_topology',{'system_id':'x','dependencies':'x,y'})
        self.assertTrue(r['findings']);self.assertFalse(r['prediction_available'])
if __name__=='__main__':unittest.main()
