"""Offline tests of actual runtime state logic (no LLM/schema validation)."""
import ast
import copy
import json
import re
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

path = Path(__file__).with_name('order_runtime_final.py')
tree = ast.parse(path.read_text())
nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
         or (isinstance(n, ast.Assign) and not any(isinstance(t, ast.Name)
         and t.id == 'ORDER_UPDATE_SCHEMA' for t in n.targets))]
namespace = dict(copy=copy, json=json, re=re, unicodedata=unicodedata, Any=Any)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
Manager = namespace['OrderStateManager']

def action(op, line=None, item=None, **kwargs):
    fields = dict(operation=op, target={'line_id': line} if line else None,
                  item=item, quantity_delta=None, apply_to_all=False,
                  exclude_add=[], exclude_remove=[], toppings_add=[], toppings_remove=[])
    fields.update(kwargs)
    return NS(**fields)

def update(*actions):
    return NS(intent='order', order_id=None, actions=list(actions))

class IndividualTests(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.m.apply(update(action('add', item=dict(item_type='burger', quantity=5,
                                                  menu='cheese_burger', type='single'))))

    def test_five_unique_units(self):
        self.assertEqual([i['line_id'] for i in self.m.state['items']], [1,2,3,4,5])
        self.assertEqual([i['quantity'] for i in self.m.state['items']], [1]*5)

    def test_two_then_different_toppings(self):
        self.m.apply(update(action('modify',1,{'menu':'bulgogi_burger'}),
                            action('modify',2,{'menu':'bulgogi_burger'})))
        self.assertEqual(self.m.last_selected_ids,[1,2])
        self.m.apply(update(action('modify',1,exclude_add=['pickle']),
                            action('modify',2,toppings_add=['bacon'])))
        items=self.m.state['items']
        self.assertEqual(sum(i['quantity'] for i in items),5)
        self.assertEqual([i['menu'] for i in items],['bulgogi_burger']*2+['cheese_burger']*3)
        self.assertEqual(items[0]['exclude'],['pickle'])
        self.assertEqual(items[0]['add_toppings'],[])
        self.assertEqual(items[1]['exclude'],[])
        self.assertEqual(items[1]['add_toppings'],['bacon'])
        self.assertTrue(all(not i['exclude'] and not i['add_toppings'] for i in items[2:]))
        prices={'cheese_burger':5000,'bulgogi_burger':4500}
        self.assertEqual(sum(prices[i['menu']]+800*len(i['add_toppings']) for i in items),24800)

    def test_subset_three_preserves_set_options(self):
        self.m.apply(update(*[action('modify',n,dict(type='set',drink='coke',drink_size='large',side='french_fries')) for n in (1,2,3)]))
        self.m.apply(update(*[action('modify',n,dict(menu='bulgogi_burger')) for n in (1,2,3)]))
        self.assertEqual([i['type'] for i in self.m.state['items']],['set']*3+['single']*2)
        self.assertTrue(all(i['drink']=='coke' for i in self.m.state['items'][:3]))

    def test_identical_items_never_remerge(self):
        self.m.apply(update(action('modify',2,exclude_add=['pickle'])))
        self.m.apply(update(action('modify',2,exclude_remove=['pickle'])))
        self.assertEqual(len(self.m.state['items']),5)

    def test_ids_not_reused(self):
        self.m.apply(update(action('remove',5)))
        self.m.apply(update(action('add',item=dict(item_type='side',quantity=1,side='cheese_stick'))))
        self.assertEqual(self.m.state['items'][-1]['line_id'],6)

    def test_invalid_batch_rollback(self):
        self.m.apply(update(action('add',item=dict(item_type='side',quantity=1,side='cheese_stick'))))
        before=copy.deepcopy(self.m.__dict__)
        with self.assertRaises(RuntimeError):
            self.m.apply(update(action('modify',1,{'menu':'bulgogi_burger'}),action('modify',6,{'menu':'bulgogi_burger'})))
        self.assertEqual(self.m.__dict__,before)

    def test_empty_modify_rejected(self):
        before=copy.deepcopy(self.m.__dict__)
        with self.assertRaises(RuntimeError): self.m.apply(update(action('modify',1)))
        self.assertEqual(self.m.__dict__,before)

    def test_positive_quantity_expands(self):
        self.m.apply(update(action('adjust_quantity',1,quantity_delta=2)))
        self.assertEqual(len(self.m.state['items']),7)
        self.assertTrue(all(i['quantity']==1 for i in self.m.state['items']))

    def test_reset_clears_selection(self):
        self.m.reset()
        self.assertEqual(self.m.last_selected_ids,[])
        self.assertEqual(self.m.state['items'],[])

class RepairTests(unittest.TestCase):
    def repair(self, item):
        return namespace['repair_add_items']({'intent':'order','actions':[{'operation':'add','item':item}]})

    def test_reported_null_kind(self):
        item={'item_type':None,'quantity':5,'menu':'cheese_burger','type':'single'}
        fixed,warnings=self.repair(item)
        patch=fixed['actions'][0]['item']
        self.assertEqual(patch['item_type'],'burger')
        self.assertIsNone(item['item_type'])
        m=Manager()
        m.apply(update(action('add',item=patch)))
        self.assertEqual(len(m.state['items']),5)
        self.assertTrue(all(i['menu']=='cheese_burger' and i['type']=='single' for i in m.state['items']))
        self.assertTrue(warnings)

    def test_drink_kind_without_invented_size(self):
        fixed,_=self.repair({'drink':'coke','quantity':1})
        patch=fixed['actions'][0]['item']
        self.assertEqual(patch['item_type'],'drink')
        self.assertNotIn('drink_size',patch)

    def test_side_kind(self):
        fixed,_=self.repair({'side':'cheese_stick','quantity':1})
        self.assertEqual(fixed['actions'][0]['item']['item_type'],'side')

    def test_missing_item_not_fabricated(self):
        with self.assertRaises(RuntimeError): self.repair(None)
        with self.assertRaises(RuntimeError): self.repair({})

    def test_ambiguous_or_conflicting_kind_rejected(self):
        with self.assertRaises(RuntimeError): self.repair({'drink':'coke','side':'cheese_stick'})
        with self.assertRaises(RuntimeError): self.repair({'item_type':'side','menu':'cheese_burger','side':'cheese_stick'})

class SubsetTests(unittest.TestCase):
    def setUp(self):
        self.m=Manager()
        self.m.apply(update(action('add',item=dict(item_type='burger',quantity=5,menu='cheese_burger',type='single'))))
        self.data={'intent':'order','actions':[{'operation':'modify','target':{'line_id':i},'item':{'menu':'bulgogi_burger'}} for i in range(1,6)]}
    def check(self,text,data=None):
        return namespace['reconcile_subset_count'](text,self.m.state,self.m.last_selected_ids,data or self.data)
    def test_reported_five_for_two(self):
        fixed,warnings=self.check('그중 두 개만 불고기 버거로 바꿔주세요')
        self.assertEqual([a['target']['line_id'] for a in fixed['actions']],[1,2])
        self.assertEqual(len(self.data['actions']),5)
        self.assertTrue(warnings)
    def test_three_numeric(self):
        fixed,_=self.check('그중 3개만 불고기로 변경')
        self.assertEqual(len(fixed['actions']),3)
    def test_mixed_items_rejected(self):
        self.m.state['items'][4]['menu']='chicken_burger'
        with self.assertRaises(RuntimeError): self.check('그중 두 개만 변경')
    def test_mixed_changes_rejected(self):
        self.data['actions'][4]['item']['menu']='chicken_burger'
        with self.assertRaises(RuntimeError): self.check('그중 두 개만 변경')
    def test_underselection_rejected(self):
        self.data['actions']=self.data['actions'][:1]
        with self.assertRaises(RuntimeError): self.check('그중 두 개만 변경')
    def test_no_arbitrary_truncation(self):
        with self.assertRaises(RuntimeError): self.check('치즈버거 두 개만 변경')

if __name__=='__main__':
    unittest.main(verbosity=2)
