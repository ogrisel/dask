from __future__ import absolute_import, division, print_function

import itertools
import math
import tempfile
import inspect
import gzip
import zlib
import bz2
import os

from fnmatch import fnmatchcase
from glob import glob
from collections import Iterable, Iterator, defaultdict
from functools import wraps, partial


from toolz import (merge, frequencies, merge_with, take, reduce,
                   join, reduceby, valmap, count, map, partition_all, filter,
                   remove, pluck, groupby, topk)
import toolz
from ..utils import tmpfile, ignoring
with ignoring(ImportError):
    from cytoolz import (frequencies, merge_with, join, reduceby,
                         count, pluck, groupby, topk)

from ..multiprocessing import get as mpget
from ..core import istask, get_dependencies, reverse_dict
from ..optimize import fuse, cull, inline
from ..compatibility import (apply, BytesIO, unicode, urlopen, urlparse, quote,
        unquote)
from ..context import _globals

names = ('bag-%d' % i for i in itertools.count(1))
tokens = ('-%d' % i for i in itertools.count(1))
load_names = ('load-%d' % i for i in itertools.count(1))

no_default = '__no__default__'


def lazify_task(task, start=True):
    """
    Given a task, remove unnecessary calls to ``list``

    Example
    -------

    >>> task = (sum, (list, (map, inc, [1, 2, 3])))  # doctest: +SKIP
    >>> lazify_task(task)  # doctest: +SKIP
    (sum, (map, inc, [1, 2, 3]))
    """
    if not istask(task):
        return task
    head, tail = task[0], task[1:]
    if not start and head in (list, reify):
        task = task[1]
        return lazify_task(*tail, start=False)
    else:
        return (head,) + tuple([lazify_task(arg, False) for arg in tail])


def lazify(dsk):
    """
    Remove unnecessary calls to ``list`` in tasks

    See Also:
        ``dask.bag.core.lazify_task``
    """
    return valmap(lazify_task, dsk)


def inline_singleton_lists(dsk):
    """ Inline lists that are only used once

    >>> d = {'b': (list, 'a'),
    ...      'c': (f, 'b', 1)}     # doctest: +SKIP
    >>> inline_singleton_lists(d)  # doctest: +SKIP
    {'c': (f, (list, 'a'), 1)}

    Pairs nicely with lazify afterwards
    """

    dependencies = dict((k, get_dependencies(dsk, k)) for k in dsk)
    dependents = reverse_dict(dependencies)

    keys = [k for k, v in dsk.items() if istask(v) and v
                                      and v[0] is list
                                      and len(dependents[k]) == 1]
    return inline(dsk, keys, inline_constants=False)


def optimize(dsk, keys):
    """ Optimize a dask from a dask.bag """
    dsk2 = cull(dsk, keys)
    dsk3 = fuse(dsk2)
    dsk4 = inline_singleton_lists(dsk3)
    dsk5 = lazify(dsk4)
    return dsk5


def get(dsk, keys, get=None, **kwargs):
    """ Get function for dask.bag """
    get = get or _globals['get'] or mpget

    dsk2 = optimize(dsk, keys)

    return get(dsk2, keys, **kwargs)
_get = get


def list2(seq):
    """ Another list function that won't be removed by lazify """
    return list(seq)


def to_textfiles(b, path, name_function=str):
    """ Write bag to disk, one filename per partition, one line per element

    **Paths**: This will create one file for each partition in your bag. You
    can specify the filenames in a variety of ways.

    Use a globstring

    >>> b.to_textfiles('/path/to/data/*.json.gz')  # doctest: +SKIP

    The * will be replaced by the increasing sequence 1, 2, ...

    ::

        /path/to/data/0.json.gz
        /path/to/data/1.json.gz

    Use a globstring and a ``name_function=`` keyword argument.  The
    name_function function should expect an integer and produce a string.

    >>> from datetime import date, timedelta
    >>> def name(i):
    ...     return str(date(2015, 1, 1) + i * timedelta(days=1))

    >>> name(0)
    '2015-01-01'
    >>> name(15)
    '2015-01-16'

    >>> b.to_textfiles('/path/to/data/*.json.gz', name_function=name)  # doctest: +SKIP

    ::

        /path/to/data/2015-01-01.json.gz
        /path/to/data/2015-01-02.json.gz
        ...

    You can also provide an explicit list of paths.

    >>> paths = ['/path/to/data/alice.json.gz', '/path/to/data/bob.json.gz', ...]  # doctest: +SKIP
    >>> b.to_textfiles(paths) # doctest: +SKIP

    **Compression**: Filenames with extensions corresponding to known
    compression algorithms (gz, bz2) will be compressed accordingly.
    """
    if isinstance(path, (str, unicode)):
        if '*' in path:
            paths = [path.replace('*', name_function(i))
                     for i in range(b.npartitions)]
        else:
            paths = [os.path.join(path, '%s.part' % name_function(i))
                     for i in range(b.npartitions)]
    elif isinstance(path, (tuple, list, set)):
        assert len(path) == b.npartitions
        paths = path
    else:
        raise ValueError("Path should be either\n"
                "1.  A list of paths -- ['foo.json', 'bar.json', ...]\n"
                "2.  A directory -- 'foo/\n"
                "3.  A path with a * in it -- 'foo.*.json'")

    name = next(names)
    dsk = dict(((name, i), (write, (b.name, i), path))
            for i, path in enumerate(paths))

    return Bag(merge(b.dask, dsk), name, b.npartitions)


class Item(object):
    def __init__(self, dsk, key):
        self.dask = dsk
        self.key = key

    def compute(self, **kwargs):
        return get(self.dask, self.key, **kwargs)

    def apply(self, func):
        name = next(names)
        dsk = {name: (func, self.key)}
        return Item(merge(self.dask, dsk), name)

    __int__ = __float__ = __complex__ = __bool__ = compute


class Bag(object):
    """ Parallel collection of Python objects

    Example
    -------

    Create Bag from sequence

    >>> import dask.bag as db
    >>> b = db.from_sequence(range(5))
    >>> list(b.filter(lambda x: x % 2 == 0).map(lambda x: x * 10))  # doctest: +SKIP
    [0, 20, 40]

    Create Bag from filename or globstring of filenames

    >>> b = db.from_filenames('/path/to/mydata.*.json.gz').map(json.loads)  # doctest: +SKIP

    Create manually (expert use)

    >>> dsk = {('x', 0): (range, 5),
    ...        ('x', 1): (range, 5),
    ...        ('x', 2): (range, 5)}
    >>> b = Bag(dsk, 'x', npartitions=3)

    >>> sorted(b.map(lambda x: x * 10))  # doctest: +SKIP
    [0, 0, 0, 10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40]

    >>> int(b.fold(lambda x, y: x + y))  # doctest: +SKIP
    30
    """
    def __init__(self, dsk, name, npartitions):
        self.dask = dsk
        self.name = name
        self.npartitions = npartitions
        self.str = StringAccessor(self)

    def map(self, func):
        """ Map a function across all elements in collection

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.map(lambda x: x * 10))  # doctest: +SKIP
        [0, 10, 20, 30, 40]
        """
        name = next(names)
        if takes_multiple_arguments(func):
            func = partial(apply, func)
        dsk = dict(((name, i), (reify, (map, func, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    @property
    def _args(self):
        return (self.dask, self.name, self.npartitions)

    def _visualize(self, optimize_graph=False):
        from dask.dot import dot_graph
        if optimize_graph:
            dot_graph(optimize(self.dask, self._keys()))
        else:
            dot_graph(self.dask)

    def filter(self, predicate):
        """ Filter elements in collection by a predicate function

        >>> def iseven(x):
        ...     return x % 2 == 0

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.filter(iseven))  # doctest: +SKIP
        [0, 2, 4]
        """
        name = next(names)
        dsk = dict(((name, i), (reify, (filter, predicate, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def remove(self, predicate):
        """ Remove elements in collection that match predicate

        >>> def iseven(x):
        ...     return x % 2 == 0

        >>> import dask.bag as db
        >>> b = db.from_sequence(range(5))
        >>> list(b.remove(iseven))  # doctest: +SKIP
        [1, 3]
        """
        name = next(names)
        dsk = dict(((name, i), (reify, (remove, predicate, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def map_partitions(self, func):
        """ Apply function to every partition within collection

        Note that this requires you to understand how dask.bag partitions your
        data and so is somewhat internal.

        >>> b.map_partitions(myfunc)  # doctest: +SKIP
        """
        name = next(names)
        dsk = dict(((name, i), (func, (self.name, i)))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def pluck(self, key, default=no_default):
        """ Select item from all tuples/dicts in collection

        >>> b = from_sequence([{'name': 'Alice', 'credits': [1, 2, 3]},
        ...                    {'name': 'Bob',   'credits': [10, 20]}])
        >>> list(b.pluck('name'))  # doctest: +SKIP
        ['Alice', 'Bob']
        >>> list(b.pluck('credits').pluck(0))  # doctest: +SKIP
        [1, 10]
        """
        name = next(names)
        if isinstance(key, list):
            key = (list2, key)
        if default == no_default:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i))))
                       for i in range(self.npartitions))
        else:
            dsk = dict(((name, i), (list, (pluck, key, (self.name, i), default)))
                       for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    @classmethod
    def from_sequence(cls, *args, **kwargs):
        raise AttributeError("db.Bag.from_sequence is deprecated.\n"
                             "Use db.from_sequence instead.")

    @classmethod
    def from_filenames(cls, *args, **kwargs):
        raise AttributeError("db.Bag.from_filenames is deprecated.\n"
                             "Use db.from_filenames instead.")

    @wraps(to_textfiles)
    def to_textfiles(self, path, name_function=str):
        return to_textfiles(self, path, name_function)

    def fold(self, binop, combine=None, initial=None):
        """ Splittable reduction

        Apply binary operator on each partition to perform reduce.  Follow by a
        second binary operator to combine results

        >>> b = from_sequence(range(5))
        >>> b.fold(lambda x, y: x + y).compute()  # doctest: +SKIP
        10

        Optionally provide default arguments and special combine binary
        operator

        >>> b.fold(lambda x, y: x + y, lambda x, y: x + y, 0).compute()  # doctest: +SKIP
        10
        """
        a = next(names)
        b = next(names)
        if initial:
            dsk = dict(((a, i), (reduce, binop, (self.name, i), initial))
                            for i in range(self.npartitions))
        else:
            dsk = dict(((a, i), (reduce, binop, (self.name, i)))
                            for i in range(self.npartitions))
        dsk2 = {b: (reduce, combine or binop, list(dsk.keys()))}
        return Item(merge(self.dask, dsk, dsk2), b)

    def frequencies(self):
        """ Count number of occurrences of each distinct element

        >>> b = from_sequence(['Alice', 'Bob', 'Alice'])
        >>> dict(b.frequencies())  # doctest: +SKIP
        {'Alice': 2, 'Bob', 1}
        """
        a = next(names)
        b = next(names)
        dsk = dict(((a, i), (frequencies, (self.name, i)))
                        for i in range(self.npartitions))
        dsk2 = {(b, 0): (dictitems,
                            (merge_with, sum, list(sorted(dsk.keys()))))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)


    def topk(self, k, key=None):
        """ K largest elements in collection

        Optionally ordered by some key function

        >>> b = from_sequence([10, 3, 5, 7, 11, 4])
        >>> list(b.topk(2))  # doctest: +SKIP
        [11, 10]

        >>> list(b.topk(2, lambda x: -x))  # doctest: +SKIP
        [3, 4]
        """
        a = next(names)
        b = next(names)
        if key:
            if callable(key) and takes_multiple_arguments(key):
                key = partial(apply, key)
            func = partial(topk, key=key)
        else:
            func = topk
        dsk = dict(((a, i), (list, (func, k, (self.name, i))))
                        for i in range(self.npartitions))
        dsk2 = {(b, 0): (list, (func, k, (toolz.concat, list(dsk.keys()))))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def distinct(self):
        """ Distinct elements of collection

        Unordered without repeats.

        >>> b = from_sequence(['Alice', 'Bob', 'Alice'])
        >>> sorted(b.distinct())
        ['Alice', 'Bob']
        """
        a = next(names)
        dsk = dict(((a, i), (set, key)) for i, key in enumerate(self._keys()))
        b = next(names)
        dsk2 = {(b, 0): (apply, set.union, (list2, list(dsk.keys())))}

        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def reduction(self, perpartition, aggregate):
        """ Reduce collection with reduction operators

        Parameters
        ----------

        perpartition: function
            reduction to apply to each partition
        aggregate: function
            reduction to apply to the results of all partitions

        Example
        -------

        >>> b = from_sequence(range(10))
        >>> b.reduction(sum, sum).compute()
        45
        """
        a = next(names)
        b = next(names)
        dsk = dict(((a, i), (perpartition, (self.name, i)))
                        for i in range(self.npartitions))
        dsk2 = {b: (aggregate, list(dsk.keys()))}
        return Item(merge(self.dask, dsk, dsk2), b)

    @wraps(sum)
    def sum(self):
        return self.reduction(sum, sum)

    @wraps(max)
    def max(self):
        return self.reduction(max, max)

    @wraps(min)
    def min(self):
        return self.reduction(min, min)

    @wraps(any)
    def any(self):
        return self.reduction(any, any)

    @wraps(all)
    def all(self):
        return self.reduction(all, all)

    def count(self):
        """ Count the number of elements """
        return self.reduction(count, sum)

    def mean(self):
        """ Arithmetic mean """
        def chunk(seq):
            total, n = 0.0, 0
            for x in seq:
                total += x
                n += 1
            return total, n
        def agg(x):
            totals, counts = list(zip(*x))
            return 1.0 * sum(totals) / sum(counts)
        return self.reduction(chunk, agg)

    def var(self, ddof=0):
        """ Variance """
        def chunk(seq):
            squares, total, n = 0.0, 0.0, 0
            for x in seq:
                squares += x**2
                total += x
                n += 1
            return squares, total, n
        def agg(x):
            squares, totals, counts = list(zip(*x))
            x2, x, n = float(sum(squares)), float(sum(totals)), sum(counts)
            result = (x2 / n) - (x / n)**2
            return result * n / (n - ddof)
        return self.reduction(chunk, agg)

    def std(self, ddof=0):
        """ Standard deviation """
        return self.var(ddof=ddof).apply(math.sqrt)

    def join(self, other, on_self, on_other=None):
        """ Join collection with another collection

        Other collection must be an Iterable, and not a Bag.

        >>> people = from_sequence(['Alice', 'Bob', 'Charlie'])
        >>> fruit = ['Apple', 'Apricot', 'Banana']
        >>> list(people.join(fruit, lambda x: x[0]))  # doctest: +SKIP
        [('Apple', 'Alice'), ('Apricot', 'Alice'), ('Banana', 'Bob')]
        """
        assert isinstance(other, Iterable)
        assert not isinstance(other, Bag)
        if on_other is None:
            on_other = on_self
        name = next(names)
        dsk = dict(((name, i), (list, (join, on_other, other,
                                       on_self, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def product(self, other):
        """ Cartesian product between two bags """
        assert isinstance(other, Bag)
        name = next(names)
        n, m = self.npartitions, other.npartitions
        dsk = dict(((name, i*m + j),
                   (list, (itertools.product, (self.name, i),
                                              (other.name, j))))
                   for i in range(n) for j in range(m))
        return Bag(merge(self.dask, other.dask, dsk), name, n*m)

    def foldby(self, key, binop, initial=no_default, combine=None,
               combine_initial=no_default):
        """ Combined reduction and groupby

        Foldby provides a combined groupby and reduce for efficient parallel
        split-apply-combine tasks.

        The computation

        >>> b.foldby(key, binop, init)                        # doctest: +SKIP

        is equivalent to the following:

        >>> def reduction(group):                               # doctest: +SKIP
        ...     return reduce(binop, group, init)               # doctest: +SKIP

        >>> b.groupby(key).map(lambda (k, v): (k, reduction(v)))# doctest: +SKIP

        But uses minimal communication and so is *much* faster.

        >>> b = from_sequence(range(10))
        >>> iseven = lambda x: x % 2 == 0
        >>> add = lambda x, y: x + y
        >>> dict(b.foldby(iseven, add))                         # doctest: +SKIP
        {True: 20, False: 25}

        Key Function
        ------------

        The key function determines how to group the elements in your bag.
        In the common case where your bag holds dictionaries then the key
        function often gets out one of those elements.

        >>> def key(x):
        ...     return x['name']

        This case is so common that it is special cased, and if you provide a
        key that is not a callable function then dask.bag will turn it into one
        automatically.  The following are equivalent:

        >>> b.foldby(lambda x: x['name'], ...)  # doctest: +SKIP
        >>> b.foldby('name', ...)  # doctest: +SKIP

        Binops
        ------

        It can be tricky to construct the right binary operators to perform
        analytic queries.  The ``foldby`` method accepts two binary operators,
        ``binop`` and ``combine``.

        Binop takes a running total and a new element and produces a new total

        >>> def binop(total, x):
        ...     return total + x['amount']

        Combine takes two totals and combines them

        >>> def combine(total1, total2):
        ...     return total1 + total2

        Each of these binary operators may have a default first value for
        total, before any other value is seen.  For addition binary operators
        like above this is often ``0`` or the identity element for your
        operation.

        >>> b.foldby('name', binop, 0, combine, 0)  # doctest: +SKIP

        See also
        --------

        toolz.reduceby
        pyspark.combineByKey
        """
        a = next(names)
        b = next(names)
        if combine is None:
            combine = binop
        if initial is not no_default:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i), initial))
                        for i in range(self.npartitions))
        else:
            dsk = dict(((a, i),
                        (reduceby, key, binop, (self.name, i)))
                        for i in range(self.npartitions))
        combine2 = lambda acc, x: combine(acc, x[1])
        if combine_initial is not no_default:
            dsk2 = {(b, 0): (dictitems,
                              (reduceby,
                                0, combine2,
                                (toolz.concat, (map, dictitems, list(dsk.keys()))),
                                combine_initial))}
        else:
            dsk2 = {(b, 0): (dictitems,
                              (merge_with,
                                (partial, reduce, combine),
                                list(dsk.keys())))}
        return Bag(merge(self.dask, dsk, dsk2), b, 1)

    def take(self, k, compute=True):
        """ Take the first k elements

        Evaluates by default, use ``compute=False`` to avoid computation.
        Only takes from the first partition

        >>> b = from_sequence(range(10))
        >>> b.take(3)  # doctest: +SKIP
        (0, 1, 2)
        """
        name = next(names)
        dsk = {(name, 0): (list, (take, k, (self.name, 0)))}
        b = Bag(merge(self.dask, dsk), name, 1)
        if compute:
            return tuple(b.compute())
        else:
            return b

    def _keys(self):
        return [(self.name, i) for i in range(self.npartitions)]

    def compute(self, **kwargs):
        """ Force evaluation of bag """
        results = get(self.dask, self._keys(), **kwargs)
        if isinstance(results[0], Iterable) and not isinstance(results[0], str):
            results = toolz.concat(results)
        if isinstance(results, Iterator):
            results = list(results)
        return results

    def concat(self):
        """ Concatenate nested lists into one long list

        >>> b = from_sequence([[1], [2, 3]])
        >>> list(b)
        [[1], [2, 3]]

        >>> list(b.concat())
        [1, 2, 3]
        """
        name = next(names)
        dsk = dict(((name, i), (list, (toolz.concat, (self.name, i))))
                        for i in range(self.npartitions))
        return Bag(merge(self.dask, dsk), name, self.npartitions)

    def __iter__(self):
        return iter(self.compute())

    def groupby(self, grouper, npartitions=None, blocksize=2**20):
        """ Group collection by key function

        Note that this requires full dataset read, serialization and shuffle.
        This is expensive.  If possible you should use ``foldby``.

        >>> b = from_sequence(range(10))
        >>> dict(b.groupby(lambda x: x % 2 == 0))  # doctest: +SKIP
        {True: [0, 2, 4, 6, 8], False: [1, 3, 5, 7, 9]}

        See Also
        --------

        Bag.foldby
        """
        if npartitions is None:
            npartitions = self.npartitions

        import partd
        p = ('partd' + next(tokens),)
        try:
            dsk1 = {p: (partd.Python, (partd.Snappy, partd.File()))}
        except AttributeError:
            dsk1 = {p: (partd.Python, partd.File())}

        # Partition data on disk
        name = next(names)
        dsk2 = dict(((name, i),
                     (partition, grouper, (self.name, i),
                                 npartitions, p, blocksize))
                     for i in range(self.npartitions))

        # Barrier
        barrier_token = 'barrier' + next(tokens)
        def barrier(args):         return 0
        dsk3 = {barrier_token: (barrier, list(dsk2))}

        # Collect groups
        name = next(names)
        dsk4 = dict(((name, i),
                     (collect, grouper, i, p, barrier_token))
                    for i in range(npartitions))

        return Bag(merge(self.dask, dsk1, dsk2, dsk3, dsk4), name, npartitions)

    def to_dataframe(self, columns=None):
        """ Convert Bag to dask.dataframe

        Bag should contain tuple or dict records.

        Provide ``columns=`` keyword arg to specify column names.

        Index will not be particularly meaningful.  Use ``reindex`` afterwards
        if necessary.

        Example
        -------

        >>> import dask.bag as db
        >>> b = db.from_sequence([{'name': 'Alice',   'balance': 100},
        ...                       {'name': 'Bob',     'balance': 200},
        ...                       {'name': 'Charlie', 'balance': 300}],
        ...                      npartitions=2)
        >>> df = b.to_dataframe()

        >>> df.compute()
           balance     name
        0      100    Alice
        1      200      Bob
        0      300  Charlie
        """
        import pandas as pd
        import dask.dataframe as dd
        if columns is None:
            head = self.take(1)[0]
            if isinstance(head, dict):
                columns = sorted(head)
            elif isinstance(head, (tuple, list)):
                columns = list(range(len(head)))
        name = next(names)
        DataFrame = partial(pd.DataFrame, columns=columns)
        dsk = dict(((name, i), (DataFrame, (list2, (self.name, i))))
                   for i in range(self.npartitions))

        divisions = [None] * (self.npartitions + 1)

        return dd.DataFrame(merge(optimize(self.dask, self._keys()), dsk),
                            name, columns, divisions)


def partition(grouper, sequence, npartitions, p, nelements=2**20):
    """ Partition a bag along a grouper, store partitions on disk """
    for block in partition_all(nelements, sequence):
        d = groupby(grouper, block)
        d2 = defaultdict(list)
        for k, v in d.items():
            d2[abs(hash(k)) % npartitions].extend(v)
        p.append(d2)
    return p


def collect(grouper, group, p, barrier_token):
    """ Collect partitions from disk and yield k,v group pairs """
    d = groupby(grouper, p.get(group, lock=False))
    return list(d.items())


opens = {'gz': gzip.open, 'bz2': bz2.BZ2File}


def from_filenames(filenames):
    """ Create dask by loading in lines from many files

    Provide list of filenames

    >>> b = from_filenames(['myfile.1.txt', 'myfile.2.txt'])  # doctest: +SKIP

    Or a globstring

    >>> b = from_filenames('myfiles.*.txt')  # doctest: +SKIP

    See also:
        from_sequence: A more generic bag creation function
    """
    if isinstance(filenames, str):
        filenames = sorted(glob(filenames))

    if not filenames:
        raise ValueError("No filenames found")

    full_filenames = [os.path.abspath(f) for f in filenames]

    extension = os.path.splitext(filenames[0])[1].strip('.')
    myopen = opens.get(extension, open)

    d = dict((('load', i), (list, (myopen, fn)))
             for i, fn in enumerate(full_filenames))
    return Bag(d, 'load', len(d))


def write(data, filename):
    dirname = os.path.dirname(filename)
    if not os.path.exists(dirname):
        with ignoring(OSError):
            os.makedirs(dirname)

    ext = os.path.splitext(filename)[1][1:]
    if ext == 'gz':
        f = gzip.open(filename, 'wb')
        data = (line.encode() for line in data)
    elif ext == 'bz2':
        f = bz2.BZ2File(filename, 'wb')
        data = (line.encode() for line in data)
    else:
        f = open(filename, 'w')
    try:
        for item in data:
            f.write(item)
    finally:
        f.close()


def _get_s3_bucket(bucket_name, aws_access_key, aws_secret_key, connection,
                   anon):
    """Connect to s3 and return a bucket"""
    import boto
    if anon is True:
        connection = boto.connect_s3(anon=anon)
    elif connection is None:
        connection = boto.connect_s3(aws_access_key, aws_secret_key)
    return connection.get_bucket(bucket_name)


# we need an unmemoized function to call in the main thread. And memoized
# functions for the dask.
_memoized_get_bucket = toolz.memoize(_get_s3_bucket)


def _get_key(bucket_name, conn_args, key_name):
    bucket = _memoized_get_bucket(bucket_name, *conn_args)
    key = bucket.get_key(key_name)
    ext = key_name.split('.')[-1]
    return stream_decompress(ext, key.read())


def _parse_s3_URI(bucket_name, paths):
    assert bucket_name.startswith('s3://')
    o = urlparse('s3://' + quote(bucket_name[len('s3://'):]))
    # if path is specified
    if (paths == '*') and (o.path != '' and o.path != '/'):
        paths = unquote(o.path[1:])
    bucket_name = unquote(o.hostname)
    return bucket_name, paths


def from_s3(bucket_name, paths='*', aws_access_key=None, aws_secret_key=None,
            connection=None, anon=False):
    """ Create a Bag by loading textfiles from s3

    Each line will be treated as one element and each file in S3 as one
    partition.

    You may specify a full s3 bucket

    >>> b = from_s3('s3://bucket-name')  # doctest: +SKIP

    Or select files, lists of files, or globstrings of files within that bucket

    >>> b = from_s3('s3://bucket-name', 'myfile.json')  # doctest: +SKIP
    >>> b = from_s3('s3://bucket-name', ['alice.json', 'bob.json'])  # doctest: +SKIP
    >>> b = from_s3('s3://bucket-name', '*.json')  # doctest: +SKIP
    """
    conn_args = (aws_access_key, aws_secret_key, connection, anon)

    bucket_name, paths = normalize_s3_names(bucket_name, paths, conn_args)

    get_key = partial(_get_key, bucket_name, conn_args)

    name = next(load_names)
    dsk = dict(((name, i), (list, (get_key, k))) for i, k in enumerate(paths))
    return Bag(dsk, name, len(paths))


def normalize_s3_names(bucket_name, paths, conn_args):
    """ Normalize bucket name and paths """
    if bucket_name.startswith('s3://'):
        bucket_name, paths = _parse_s3_URI(bucket_name, paths)

    if isinstance(paths, str):
        if ('*' not in paths) and ('?' not in paths):
            return bucket_name, [paths]
        else:
            bucket = _get_s3_bucket(bucket_name, *conn_args)
            keys = bucket.list()  # handle globs

            matches = [k.name for k in keys if fnmatchcase(k.name, paths)]
            return bucket_name, matches
    else:
        return bucket_name, paths


def from_hdfs(path, hdfs=None, host='localhost', port='50070', user_name=None):
    """ Create dask by loading in files from HDFS

    Provide an hdfs directory and credentials

    >>> b = from_hdfs('home/username/data/', host='localhost', user_name='ubuntu')  # doctest: +SKIP

    Alternatively provide an instance of ``pywebhdfs.webhdfs.PyWebHdfsClient``

    >>> from pywebhdfs.webhdfs import PyWebHdfsClient  # doctest: +SKIP
    >>> hdfs = PyWebHdfsClient(host='hostname', user_name='username')  # doctest: +SKIP

    >>> b = from_hdfs('home/username/data/', hdfs=hdfs)  # doctest: +SKIP
    """
    from .. import hdfs_utils
    filenames = hdfs_utils.filenames(hdfs, path)

    if not filenames:
        raise ValueError("No files found for path %s" % path)

    name = next(names)
    dsk = dict()
    for i, fn in enumerate(filenames):
        ext = fn.split('.')[-1]
        if ext in ('gz', 'bz2'):
            dsk[(name, i)] = (stream_decompress, ext, (hdfs.read_file, fn))
        else:
            dsk[(name, i)] = (hdfs.read_file, fn)

    return Bag(dsk, name, len(dsk))


def stream_decompress(fmt, data):
    """ Decompress a block of compressed bytes into a stream of strings """
    if fmt == 'gz':
        return gzip.GzipFile(fileobj=BytesIO(data))
    if fmt == 'bz2':
        return bz2_stream(data)
    else:
        return map(bytes.decode, BytesIO(data))


def bz2_stream(compressed, chunksize=100000):
    """ Stream lines from a chunk of compressed bz2 data """
    decompressor = bz2.BZ2Decompressor()
    for i in range(0, len(compressed), chunksize):
        chunk = compressed[i: i+chunksize]
        decompressed = decompressor.decompress(chunk).decode()
        for line in decompressed.split('\n'):
            yield line + '\n'


def from_sequence(seq, partition_size=None, npartitions=None):
    """ Create dask from Python sequence

    This sequence should be relatively small in memory.  Dask Bag works
    best when it handles loading your data itself.  Commonly we load a
    sequence of filenames into a Bag and then use ``.map`` to open them.

    Parameters
    ----------

    seq: Iterable
        A sequence of elements to put into the dask
    partition_size: int (optional)
        The length of each partition
    npartitions: int (optional)
        The number of desired partitions

    It is best to provide either ``partition_size`` or ``npartitions``
    (though not both.)

    Example
    -------

    >>> b = from_sequence(['Alice', 'Bob', 'Chuck'], partition_size=2)

    See also:
        from_filenames: Specialized bag creation function for textfiles
    """
    seq = list(seq)
    if npartitions and not partition_size:
        partition_size = int(math.ceil(len(seq) / npartitions))
    if npartitions is None and partition_size is None:
        if len(seq) < 100:
            partition_size = 1
        else:
            partition_size = int(len(seq) / 100)

    parts = list(partition_all(partition_size, seq))
    name = next(load_names)
    d = dict(((name, i), part) for i, part in enumerate(parts))
    return Bag(d, name, len(d))


def from_url(urls):
    """Create a dask.bag from a url

    >>> a = from_url('http://raw.githubusercontent.com/ContinuumIO/dask/master/README.rst')  # doctest: +SKIP
    >>> a.npartitions  # doctest: +SKIP
    1

    >> a.take(8)  # doctest: +SKIP
    ('Dask\n',
     '====\n',
     '\n',
     '|Build Status| |Coverage| |Doc Status| |Gitter|\n',
     '\n',
     'Dask provides multi-core execution on larger-than-memory datasets using blocked\n',
     'algorithms and task scheduling.  It maps high-level NumPy and list operations\n',
     'on large datasets on to graphs of many operations on small in-memory datasets.\n')

    >>> b = from_url(['http://github.com', 'http://google.com'])  # doctest: +SKIP
    >>> b.npartitions  # doctest: +SKIP
    2
    """
    if isinstance(urls, str):
        urls = [urls]
    name = next(load_names)
    dsk = {}
    for i, u in enumerate(urls):
        dsk[(name, i)] = (list, (urlopen, u))
    return Bag(dsk, name, len(urls))


def dictitems(d):
    """ A pickleable version of dict.items

    >>> dictitems({'x': 1})
    [('x', 1)]
    """
    return list(d.items())


def takes_multiple_arguments(func):
    """

    >>> def f(x, y): pass
    >>> takes_multiple_arguments(f)
    True

    >>> def f(x): pass
    >>> takes_multiple_arguments(f)
    False

    >>> def f(x, y=None): pass
    >>> takes_multiple_arguments(f)
    False

    >>> def f(*args): pass
    >>> takes_multiple_arguments(f)
    True

    >>> takes_multiple_arguments(map)  # default to False
    False
    """
    try:
        spec = inspect.getargspec(func)
    except:
        return False
    if spec.varargs:
        return True
    if spec.defaults is None:
        return len(spec.args) != 1
    return len(spec.args) - len(spec.defaults) > 1


def concat(bags):
    """ Concatenate many bags together, unioning all elements

    >>> import dask.bag as db
    >>> a = db.from_sequence([1, 2, 3])
    >>> b = db.from_sequence([4, 5, 6])
    >>> c = db.concat([a, b])

    >>> list(c)
    [1, 2, 3, 4, 5, 6]
    """
    name = next(names)
    counter = itertools.count(0)
    dsk = dict(((name, next(counter)), key) for bag in bags
                                            for key in sorted(bag.dask))
    return Bag(merge(dsk, *[b.dask for b in bags]), name, len(dsk))


class StringAccessor(object):
    """ String processing functions

    Examples
    --------

    >>> import dask.bag as db
    >>> b = db.from_sequence(['Alice Smith', 'Bob Jones', 'Charlie Smith'])
    >>> list(b.str.lower())
    ['alice smith', 'bob jones', 'charlie smith']

    >>> list(b.str.match('*Smith'))
    ['Alice Smith', 'Charlie Smith']

    >>> list(b.str.split(' '))
    [['Alice', 'Smith'], ['Bob', 'Jones'], ['Charlie', 'Smith']]
    """
    def __init__(self, bag):
        self._bag = bag

    def __dir__(self):
        return sorted(set(dir(type(self)) + dir(str)))

    def _strmap(self, key, *args, **kwargs):
        return self._bag.map(lambda s: getattr(s, key)(*args, **kwargs))

    def __getattr__(self, key):
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            if key in dir(str):
                func = getattr(str, key)
                return robust_wraps(func)(partial(self._strmap, key))
            else:
                raise

    def match(self, pattern):
        """ Filter strings by those that match a pattern

        Example
        -------

        >>> import dask.bag as db
        >>> b = db.from_sequence(['Alice Smith', 'Bob Jones', 'Charlie Smith'])
        >>> list(b.str.match('*Smith'))
        ['Alice Smith', 'Charlie Smith']

        See Also
        --------
        fnmatch.fnmatch
        """
        from fnmatch import fnmatch
        return self._bag.filter(partial(fnmatch, pat=pattern))


def robust_wraps(wrapper):
    """ A weak version of wraps that only copies doc """
    def _(wrapped):
        wrapped.__doc__ = wrapper.__doc__
        return wrapped
    return _


def reify(seq):
    if isinstance(seq, Iterator):
        seq = list(seq)
    if seq and isinstance(seq[0], Iterator):
        seq = list(map(list, seq))
    return seq
