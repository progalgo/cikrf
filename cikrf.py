from asks            import Session
from asks.errors     import AsksException
from bs4             import BeautifulSoup, Tag
from collections     import OrderedDict, namedtuple
from collections.abc import Iterable
from contextlib      import asynccontextmanager
from datetime        import date as Date, datetime as DateTime
from enum            import Enum
from itertools       import accumulate, chain, repeat
from math            import inf, sqrt
from operator        import mul
from simplejsonseq   import dump, load
from socket          import gaierror as GAIError
from trio            import BrokenResourceError, CapacityLimiter, \
                            TASK_STATUS_IGNORED, open_memory_channel, \
                            open_nursery, run, sleep
from urllib.parse    import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from weakref         import WeakValueDictionary

_PARSER = 'html5lib'

def prettyobj(p, cycle, typename, **named):
    if cycle:
        p.text(typename + '(...)')
        return
    with p.group(len(typename) + 1, typename + '(', ')'):
        for i, (key, value) in enumerate(named.items()):
            if i:
                p.text(',')
                p.breakable()
            with p.group(len(key) + 1, key + '=', ''):
                p.pretty(value)

def urladjust(url, params=dict(), **named):
    parts = urlsplit(url)
    query = parse_qs(parts.query, strict_parsing=True)
    query.update(params, **named)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query, doseq=True), ''))

def normalize(string):
    assert string is not None
    return ' '.join(str(string).split()).replace('\u2013', '-')

def todate(string):
    return DateTime.strptime(normalize(string), '%d.%m.%Y').date()
def fromdate(date):
    return date.strftime('%d.%m.%Y')

def matches(string):
    def match(s): return s and normalize(s).casefold() == string
    return match

def contains(string):
    def match(s): return s and string in normalize(s)
    return match

def strings(node):
    return normalize(' '.join(s.string for s in node(string=True)))

def nodata(page):
    mess = page.find(string=matches('нет данных для построения отчета.'))
    return mess is not None

class Scope(Enum):
    COUNTRY  = '1'
    PROVINCE = '2'
    COMMUNE  = '3'
    MUNICPTY = '4'
    # SETTLMNT FIXME

class Cache:
    __slots__ = ['delay', 'rate', '_page', '_commission']

    def __init__(self, delay=0.25, rate=sqrt(2)):
        self.delay = delay
        self.rate = rate
        self._page = WeakValueDictionary()
        self._commission = WeakValueDictionary()

    def _backoff(self):
        return accumulate(chain([self.delay], repeat(self.rate)), mul)

    async def _download(self, session, url):
        for delay in self._backoff():
            try:
                res = await session.get(url, timeout=60, connection_timeout=15)
            except AsksException:
                pass
            except BrokenResourceError:
                pass
            except ConnectionError:
                pass
            except GAIError:
                pass
            else:
                # The server raises 404(!) on parameter error
                if res.status_code // 100 == 2: break
            await sleep(delay)
        encoding = (res.encoding.lower()
                    if 'charset' in res.headers.get('content-type')
                    else None) # FIXME unreliable (change in asks?)
        return res.content, encoding

    async def page(self, session, url):
        page = self._page.get(url)
        if page is None:
            content, encoding = await self._download(session, url)
            page = self._page[url] = BeautifulSoup(
                content, _PARSER, from_encoding=encoding)
        return page

    def commission(self, parent, url):
        comm = self._commission.get(url)
        if comm is None:
            comm = self._commission[url] = Commission(parent, url)
        assert comm.parent == parent
        return comm

_Result = namedtuple('_Result', ['records', 'votes'])
class Result(_Result):
    __slots__ = []
    def __pretty__(self, p, cycle):
        prettyobj(p, cycle, type(self).__qualname__,
                  records=self.records, votes=self.votes)

_NamedResult = namedtuple('_NamedResult', ['records', 'votes', 'name'])
class NamedResult(_NamedResult):
    __slots__ = []
    def __pretty__(self, p, cycle):
        prettyobj(p, cycle, type(self).__qualname__,
                  records=self.records, votes=self.votes,
                  name=self.name)

_Row = namedtuple('_Row', ['number', 'name', 'value'])
class Row(_Row):
    __slots__ = []
    def __pretty__(self, p, cycle):
        prettyobj(p, cycle, type(self).__qualname__,
                  number=self.number, name=self.name, value=self.value)

class Commission:
    __slots__ = ['parent', 'url', '_cache', '_page', '__weakref__']

    def __init__(self, parent, url, *, cache=None):
        if cache is None:
            cache = parent._cache

        self.parent = parent
        self.url    = url
        self._cache = cache
        self._page  = dict()

    def __repr__(self):
        return '{}(parent={!r}, url={!r})'.format(
            type(self).__qualname__, self.parent, self.url)

    def __pretty__(self, p, cycle):
        prettyobj(p, cycle, type(self).__qualname__,
                  parent=self.parent, url=self.url)

    @property
    def level(self):
        if self.parent is not None:
            return self.parent.level + 1
        else:
            return 0

    async def page(self, session, type):
        assert isinstance(type, int)
        page = self._page.get(type)
        if page is None:
            page = self._page[type] = await self._cache.page(
                session, urladjust(self.url, type=type))
        return page

    @staticmethod
    def _parsetypes(page):
        types = OrderedDict()
        pivot = page.find('img', src='img/form.gif')
        if pivot is None:
            return types

        rows = pivot.find_parent('table')('tr')
        category = None
        for row in rows:
            if (not isinstance(row, Tag) or
                row.find(class_='headers') is not None or
                row.find(class_='folder') is not None):
                continue

            a = row.find('a')
            s = strings(row)
            if a is not None:
                t = parse_qs(urlsplit(a['href']).query).get('type', [None])[0]
                if t is None: continue
                t = int(t)
                assert types.get(t) is None
                types[t] = [category, normalize(s)]
            elif s:
                category = normalize(s)

        return types

    async def types(self, session):
        page = await self.page(session, 0)
        return self._parsetypes(page)

    @staticmethod
    def _parsehorz(table):
        rows = [tr('td') for tr in table('tr')]
        assert all(len(row) <= 3 for row in rows)

        # Sort into separator and data lines
        seps = [i for i, row in enumerate(rows)
                  if len(row) < 3 or not row[2](string=normalize)]
        data = [Row(number=strings(row[0]),
                    # strip the (inconsistent) candidate numbering
                    name=strings(row[1]).lstrip('0123456789. '),
                    # cells sometimes include percentages as well,
                    # so use only the first string node
                    value=int(row[2].find(string=True).string))
                for i, row in enumerate(rows) if i not in seps]

        # Find first non-leading separator group
        start = i = 0
        while start < len(seps):
            if seps[start] != start: break
            start += 1
        while i < len(seps) - start:
            if seps[start+i] != seps[start] + i: break
            i += 1

        return data, seps, start, start+i

    @staticmethod
    def _parsevert(table, head, seps):
        rows = [tr('td') for tr in table('tr')]
        data = (row for i, row in enumerate(rows) if i not in seps)

        assert rows[0][0].find(string=normalize)  # child names
        comms = [strings(td) for td in rows[0]]
        datas = [[h._replace(value=int(v.find(string=True).string))
                  for h, v in zip(head, col)]
                 for col in zip(*data)]

        return comms, datas

    @classmethod
    def _parsesingle(cls, page):
        tabs = page(cellpadding='2')
        if not tabs:
            # page is empty or refers to children
            assert (nodata(page) or
                    page(string=contains('необходимо перейти')))
            return None

        records, votes = [], []

        # Main table
        data, seps, start, end = cls._parsehorz(tabs[0])
        if start < len(seps):  # at least two data parts
            records += data[: seps[start]-start]
            votes   += data[seps[end-1]-end+1 :]
            tabs = tabs[1:]
        elif not data:  # empty table
            tabs = tabs[1:]

        # Supplementary table
        if tabs:
            data, seps, start, end = cls._parsehorz(tabs[0])
            assert end == len(seps)  # only one data part
            records += data
            tabs = tabs[1:]

        assert not tabs
        return Result(records=records, votes=votes)

    async def single(self, session, type):
        page = await self.page(session, type)
        return self._parsesingle(page)

    @classmethod
    def _parseaggregate(cls, page):
        tabs = page(cellpadding='2')
        if not tabs:
            # page is empty or refers to children
            assert (nodata(page) or
                    page(string=contains('необходимо перейти')))
            return OrderedDict()

        comms, recordss, votess = [], [], []

        # Main table: left table is titles, right is data per child
        head, seps, start, end = cls._parsehorz(tabs[0])
        assert seps[0] == 0    # first row is child names
        assert normalize(tabs[0].find(string=normalize).string).casefold() == "сумма"
        if len(tabs) == 1:     # aggregate page on bottom level
            return OrderedDict()

        if start < len(seps):  # at least two data parts
            comms, datas = cls._parsevert(tabs[1], head, seps)
            recordss += [d[: seps[start]-start] for d in datas]
            votess   += [d[seps[end-1]-end+1 :] for d in datas]
            tabs = tabs[2:]
        elif not head:  # empty table
            assert not tabs[1](string=normalize)
            tabs = tabs[2:]

        # Supplementary table
        if tabs:
            if len(tabs[0]('td')) == 1:  # title
                tabs = tabs[1:]

            assert len(tabs) >= 2
            if tabs[1]('td'):            # non-empty right table
                head, seps, start, end = cls._parsehorz(tabs[0])
                assert end == len(seps)  # only one data part

                comms_, datas = cls._parsevert(tabs[1], head, seps)
                if comms_ == comms:      # same child names
                    for records, data in zip(recordss, datas):
                        records += data

            tabs = tabs[2:]

        assert not tabs
        return OrderedDict(
            (comm, Result(records=records, votes=votes))
            for comm, records, votes
            in zip(comms, recordss, votess))

    async def aggregate(self, session, type):
        page = await self.page(session, type)
        return self._parseaggregate(page)

    async def results(self, session):
        types = await self.types(session)
        results = OrderedDict()

        async def fetch(type, name):
            res = await self.single(session, type)
            if res is not None:
                results[type] = NamedResult(
                    name=name, **res._asdict())

        async with open_nursery() as nursery:
            for type, name in types.items():
                assert len(name) == 2
                if (name[0] is None or
                    name[0].casefold() != 'результаты выборов' or
                    name[1].casefold().startswith('сводн')):
                    continue
                results[type] = None
                nursery.start_soon(fetch, type, name)
                await sleep(0)
            while nursery.child_tasks:
                await sleep(0)

        return results

    async def name(self, session):
        page = await self.page(session, 0) # FIXME Any cached page would work
        crumbs = page.find('table', height='80%').find('td')('a')
        # If the crumbs are absent, or if one of the crumbs is the
        # empty string, we can't be sure we got them right, so use
        # the fallback method.
        if len(crumbs)-1 == self.level:
            return normalize(crumbs[-1].string)

        page = await self.page(session, 0)
        caption = page.find(string=[
            matches('наименование комиссии'),
            matches('наименование избирательной комиссии')])
        if caption is None:
            assert nodata(page)
            return None
        return normalize(caption.find_parent('td')
                                .find_next_sibling('td')
                                .string)

    async def path(self, session):
        if self.parent is not None:
            ppath = await self.parent.path(session)
        else:
            ppath = []
        return ppath + [await self.name(session)]

    async def children(self, session):
        page = await self.page(session, 0) # FIXME Any cached page would work
        return (self._cache.commission(
                self, urljoin(self.url, o['value']))
                for o in page('option') if o.attrs.get('value'))

    @asynccontextmanager
    async def walk(self, session, depth=inf):
        send, recv = open_memory_channel(0)

        async with recv, open_nursery() as nursery:
            async def visit(send, comm, depth):
                assert depth > 0
                async with send:
                    await send.send(comm)
                    children = await comm.children(session)
                    if depth <= 1: return
                    for child in children:
                        nursery.start_soon(visit,
                                           send.clone(),
                                           child,
                                           depth - 1)
                        await sleep(0)

            nursery.start_soon(visit, send, self, depth)
            yield recv

class Election(Commission):
    __slots__ = ['title', 'place', 'root']

    def __init__(self, url, title=None, place=None, *, cache=None):
        if cache is None:
            cache = Cache()

        super().__init__(None, url, cache=cache)
        self.title = title
        self.place = place

    def __repr__(self):
        return '{}(url={!r}, title={!r}, place={!r})'.format(
            type(self).__qualname__,
            self.url, self.title, self.place)

    def __pretty__(self, p, cycle):
        prettyobj(p, cycle, type(self).__qualname__, url=self.url,
                  title=self.title, place=self.place)

    _CONTEXT = 'http://example.org/election.jsonld' # FIXME

    def tojson(self):
        return {'@context': self._CONTEXT, 'url': self.url,
                'title': self.title, 'place': self.place}

    @classmethod
    def fromjson(cls, data):
        ctx = data.pop('@context', None)
        if ctx and ctx != cls._CONTEXT:
            raise ValueError('Invalid @context for election JSON')
        return cls(**data)

    async def date(self, session):
        page = await self.page(session, 0)
        capt = page.find(string=matches('дата голосования'))
        if capt is None:
            assert nodata(page)
            return None
        return todate(capt.find_parent('td')
                          .find_next_sibling('td')
                          .string)

    @classmethod
    async def search(cls, session, start=Date(1991, 6, 12), end=None, *,
                     scope=list(Scope), cache=None): # FIXME parameter order?

        if end is None:
            end = Date.today() # FIXME reconsider this default?
        else:
            # Python convention prescribes half-open ranges
            end -= Date.resolution
        scope = ([Scope(s).value for s in scope]
             if isinstance(scope, Iterable)
             else [Scope(scope).value])

        SEARCH  = 'http://www.vybory.izbirkom.ru/region/izbirkom'
        payload = {
            'action'     : 'search_by_calendar',
            'start_date' : fromdate(start),
            'end_date'   : fromdate(end),
            'urovproved' : scope,
            'vidvibref'  : 'all', # FIXME multiple choice: all; 0 = Референдум; 1 = Выборы на должность; 2 = Выборы депутата; 3 = Отзыв депутата; 4 = Отзыв должностного лица
            'vibtype'    : 'all',
            'sxemavib'   : 'all',
            'region'     : '0', # FIXME whole country
        }
        res = await session.post(SEARCH, data=payload)
        # FIXME Proper encoding detection as in Cache.page()
        doc = BeautifulSoup(res.text, _PARSER)

        place = []
        for a in doc('a', class_='vibLink'):
            cell  = a.find_parent('tr').find('td')
            anode = cell.find('b')
            if anode is not None:
                place = ([normalize(anode.string)]
                     if anode.string != 'Российская Федерация'
                     else [])
            dnode = cell.find(string=True, recursive=False)
            if dnode is not None and dnode.strip():
                assert len(place) >= 1
                place = place[:1] + [dnode.strip()]

            yield cls(url=urladjust(urljoin(SEARCH, a['href']),
                                    sub_region=['99']),
                      title=normalize(a.string),
                      place=list(place),
                      cache=cache)

# FIXME test code

from asks import init as init_asks
from collections import defaultdict as DefaultDict
from contextlib import contextmanager
from os import getenv, get_terminal_size
from os.path import exists
from pretty import pprint
from random import seed, shuffle
from sys import stdout, stderr
from textwrap import shorten
from traceback import print_exc
from trio import WouldBlock

def report(done, pending, last=None):
    global lastrep
    message = (('{done} done, {pending} pending: {last}'
                if last is not None
                else '{done} done, {pending} pending')
               .format(done=done, pending=pending, last=last))
    print('\r\033[K' + shorten(message, width=w, placeholder='...'),
          end='', file=stderr, flush=True)
def clear():
    print('\r\033[K', end='', file=stderr, flush=True)

@contextmanager
def exceptions(what):
    try:
        yield
    except Exception as err:
        print('Error processing {}'.format(what), file=stderr)
        print_exc()
        stderr.flush()
        raise

async def collect_types(session, roots):
    types = DefaultDict(set)
    tsets = set()
    done  = 0
    last  = None

    async def visit(title, comm, limit, *,
                    task_status=TASK_STATUS_IGNORED):
        nonlocal done, last
        async with limit:
            task_status.started()
            with exceptions(comm.url):
                ts = [(t, n) for t, (c, n)
                      in (await comm.types(session)).items()
                      if c.casefold() == 'результаты выборов']
                for t, n in ts: types[t].add(n)
                tsets.add(tuple(t for t, n in ts))

                sing = [await comm.single(session, t)
                    for t, n in ts if not n.startswith('Сводн')]
                aggr = [await comm.aggregate(session, t)
                    for t, n in ts if n.startswith('Сводн')]

                pprint((comm.url, await comm.path(session), await comm.results(session)), max_width=160)

                skeys = [frozenset(v.name for v in res.votes)
                     for res in sing if res]
                akeys = [frozenset(v.name for v in ress.popitem()[1].votes)
                     for ress in aggr if ress]
                assert list(sorted(set(skeys), key=str)) == list(sorted(skeys, key=str))
                assert list(sorted(set(akeys), key=str)) == list(sorted(akeys, key=str))
                assert set(akeys) <= set(skeys)

                last = ('/'.join(c if c is not None else '!'
                             for c in await comm.path(session)) +
                    ': ' + title)
                done += 1

    async def traverse(nursery, root, limit):
        title = str(await root.date(session)) + ' ' + root.title
        with exceptions(root.url):
            async with root.walk(session, 2) as children:
                async for comm in children:
                    await nursery.start(visit, title, comm, limit)

    try:
        limit = CapacityLimiter(100)
        async with open_nursery() as nursery:
            for root in roots:
                nursery.start_soon(traverse, nursery, root, limit)
                report(done, len(nursery.child_tasks), last)
                await sleep(0)
            while nursery.child_tasks:
                report(done, len(nursery.child_tasks), last)
                await sleep(0)
    finally:
        clear()
        pprint(dict(types), max_width=w)
        pprint(list(map(list, tsets)), max_width=w)

async def main():
    global w
    w = getenv('COLUMNS', get_terminal_size(stderr.fileno()).columns)
    params = {
#		'start': Date(2003,1,1),
#		'end': Date(2004,1,1),
#		'scope': Scope.COUNTRY,
    }
    filename = 'elections.jsonseq'

    async with Session(connections=100) as session:
        if not exists(filename):
            with open(filename, 'w', encoding='utf-8') as fp:
                async for e in Election.search(session, **params):
                    dump([e.tojson()], fp, flush=True, ensure_ascii=False, indent=2)

        with open(filename, 'r') as fp:
            els = list(Election.fromjson(obj) for obj in load(fp))

        seed(57)
        shuffle(els)

        await collect_types(session, els)

#		elec = els[-2]
#		pprint(await elec.single(session, 226), max_width=w)
#		print()
#		pprint(await elec.aggregate(session, 227), max_width=w)
#		return

#		url = "http://www.vybory.izbirkom.ru/region/izbirkom?action=show&vrn=411401372131&region=11&prver=0&pronetvd=null&sub_region=99"
#		for i, e in enumerate(els):
#			if e.url == url: break
#		els = els[i:]

if __name__ == '__main__':
    init_asks('trio')
    run(main)
