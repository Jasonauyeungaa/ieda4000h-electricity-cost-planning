import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from . import core
class OptimizationTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.example=json.loads(next((core.DATA/'imports').glob('forecast_*.json')).read_text())
 def setUp(self):self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.p=patch.object(core,'DATA',self.root);self.p.start();self.file=self.root/'forecast.json';self.file.write_text(json.dumps(self.example))
 def tearDown(self):self.p.stop();self.t.cleanup()
 def test_detection_and_rejection(self):
  self.assertEqual(len(core.discover()['files']),1);(self.root/'bad.json').write_text('{}');self.assertEqual(len(core.discover()['errors']),1)
 def test_actuals_never_change_plan(self):
  a=core.solve('forecast.json');d=copy.deepcopy(self.example);d['evaluation_actuals']=[1000.]*48;self.file.write_text(json.dumps(d));b=core.solve('forecast.json')
  for x,y in zip(a['plans'],b['plans']):self.assertEqual(x['plan']['schedule'],y['plan']['schedule']);self.assertEqual(x['forecast_cost'],y['forecast_cost'])
  self.assertNotEqual(a['actual_saving'],b['actual_saving'])
 def test_flat_tariff_zero_saving(self):
  r=core.solve('forecast.json',tariff='flat');self.assertAlmostEqual(r['predicted_saving'],0.,places=7);self.assertTrue(all(x['check']['verdict']=='PASS' for x in r['plans']))
 def test_missing_actual_pending(self):
  d=copy.deepcopy(self.example);d['evaluation_actuals']=[None]*48;self.file.write_text(json.dumps(d));self.assertIsNone(core.solve('forecast.json')['actual_saving'])
 def test_invalid_data_rejected(self):
  for fun in [lambda d:d.update(price_unit='AUD/kWh'),lambda d:d['slots'][1].update(target_start=d['slots'][0]['target_start']),lambda d:d['slots'][0].update(price=float('nan'))]:
   d=copy.deepcopy(self.example);fun(d)
   with self.assertRaises(ValueError):core.validate(d)
 def test_path_escape_rejected(self):
  with self.assertRaises(ValueError):core.load('../outside.json')
 def test_frozen_forecast_fixture_loadable_from_outputs(self):
  out=self.root/'outputs';out.mkdir();(out/'forecast_fixture.json').write_text(json.dumps(self.example))
  d,sha=core.load('outputs/forecast_fixture.json');self.assertEqual(d['schema'],'electricity.forecast.v2')
 def test_run_result_rejected_as_input(self):
  out=self.root/'outputs';out.mkdir();(out/'schedule_x.json').write_text(json.dumps(self.example))
  with self.assertRaises(ValueError):core.load('outputs/schedule_x.json')
 def test_cross_midnight(self):
  import datetime as dt
  d=copy.deepcopy(self.example);start=dt.datetime.fromisoformat(d['forecast_origin'])+dt.timedelta(hours=19,minutes=30);d['forecast_origin']=d['input_cutoff']=start.isoformat()
  for i,r in enumerate(d['slots']):r['target_start']=(start+dt.timedelta(minutes=30*i)).isoformat();r['target_end']=(start+dt.timedelta(minutes=30*(i+1))).isoformat()
  self.file.write_text(json.dumps(d));self.assertTrue(all(x['check']['verdict']=='PASS' for x in core.solve('forecast.json')['plans']))

 def test_hedge_settlement_and_plan_independence(self):
  original = core.solve('forecast.json')
  for price, expected_payoff in [(400, 36), (50, -6)]:
   forecast = copy.deepcopy(self.example)
   forecast['evaluation_actuals'] = [price] * 48
   self.file.write_text(json.dumps(forecast))
   run = core.solve('forecast.json', hedge_strike=100)
   row = next(row for row in run['hedge']['scenarios'] if row['scenario'] == 'Observed replay')
   self.assertAlmostEqual(run['hedge']['hedged_mwh'], 0.12)
   self.assertAlmostEqual(row['hedge_payoff_aud'], expected_payoff)
   self.assertAlmostEqual(row['schedule_and_hedge_aud'],
                          run['plans'][1]['actual_cost'] - expected_payoff)
   self.assertEqual([plan['plan']['schedule'] for plan in run['plans']],
                    [plan['plan']['schedule'] for plan in original['plans']])

 def test_hedge_requires_linked_tariff_and_valid_coverage(self):
  with self.assertRaisesRegex(ValueError, 'wholesale-linked'):
   core.solve('forecast.json', tariff='flat', hedge_strike=100)
  with self.assertRaisesRegex(ValueError, 'coverage'):
   core.solve('forecast.json', hedge_strike=100, hedge_coverage=1.1)
if __name__=='__main__':unittest.main()
