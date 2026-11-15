// Run with: node tests/formula_test.js
const assert = require('assert');
const { Sheet, format } = require('../static/formula.js');
function ev(cells, key) { return format(new Sheet(cells).get(key)); }
const t = [
  [{ A1: '=1+2*3' }, 'A1', '7'],
  [{ A1: '=(1+2)*3' }, 'A1', '9'],
  [{ A1: '=2^3^2' }, 'A1', '64'],
  [{ A1: '=-2^2' }, 'A1', '4'],
  [{ A1: '=50%' }, 'A1', '0.5'],
  [{ A1: '=0.1+0.2' }, 'A1', '0.3'],
  [{ A1: '5', A2: '10', A3: '=SUM(A1:A2)' }, 'A3', '15'],
  [{ A1: '5', A2: '10', A3: 'x', A4: '=SUM(A1:A3)' }, 'A4', '15'],
  [{ A1: '5', A2: '10', A3: '=AVERAGE(A1:A2)' }, 'A3', '7.5'],
  [{ A1: '5', A2: '10', A3: '=MAX(A1:A2,100)' }, 'A3', '100'],
  [{ A1: '5', A2: '10', A3: '=MIN(A1:A2)' }, 'A3', '5'],
  [{ A1: '5', A2: 'a', A3: '=COUNT(A1:A2)' }, 'A3', '1'],
  [{ A1: '150', B1: '=IF(A1>100,"High","Low")' }, 'B1', 'High'],
  [{ A1: '50', B1: '=IF(A1>100,"High","Low")' }, 'B1', 'Low'],
  [{ A1: '=ROUND(2.567,2)' }, 'A1', '2.57'],
  [{ A1: '=ROUND(-2.5)' }, 'A1', '-3'],
  [{ A1: '=ABS(-4)+SQRT(16)' }, 'A1', '8'],
  [{ A1: 'Hi', B1: '=CONCAT(A1," ","there")' }, 'B1', 'Hi there'],
  [{ A1: 'Hi', B1: '=A1&" you"' }, 'B1', 'Hi you'],
  [{ A1: '=1/0' }, 'A1', '#DIV/0!'],
  [{ A1: '=B1', B1: '=A1' }, 'A1', '#CIRC!'],
  [{ A1: '=NOPE(1)' }, 'A1', '#NAME?'],
  [{ A1: '=1+' }, 'A1', '#ERROR!'],
  [{ A1: '=(1+2' }, 'A1', '#ERROR!'],
  [{ A1: 'abc', A2: '=A1+1' }, 'A2', '#VALUE!'],
  [{ A1: '=B1*2', B1: '=C1+1', C1: '4' }, 'A1', '10'],
  [{ A1: '2', A2: '=$A$1*3' }, 'A2', '6'],
  [{ A1: '=sum(B1:B2)', B1: '1', B2: '2' }, 'A1', '3'],
  [{ A1: '=1=1' }, 'A1', 'TRUE'],
  [{ A1: '=2<>2' }, 'A1', 'FALSE'],
  [{ A1: '=SQRT(-1)' }, 'A1', '#NUM!'],
  [{ A1: '=SUM(B1:B2)', B1: '=1/0', B2: '1' }, 'A1', '#DIV/0!'],
  [{ A1: '=B1', }, 'A1', ''],
  [{ A1: '=__proto__' }, 'A1', '#ERROR!'],
];
let n = 0;
for (const [cells, key, want] of t) { assert.strictEqual(ev(cells, key), want, JSON.stringify(cells) + ' -> ' + ev(cells, key) + ' (wanted ' + want + ')'); n++; }
console.log('formula tests passed:', n);
