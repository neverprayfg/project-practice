# Jngen reference for Agent4

This prebuilt reference contains every English document from `jngen/doc/` and every source file from `jngen/examples/`. It is the only jngen document loaded by Agent4 at runtime.


--- BEGIN DOCUMENT: array.md ---

## Arrays

Jngen provides a template class *TArray&lt;T>* which is derived from *std::vector&lt;T>* and implements all its functionality... and some more handy things like single-argument sorting (*a.sort()*) , in-place generating of random arrays (*Array::random(n, maxValue)*) and more.

There are several typedefs for convenience:
```cpp
typedef TArray<int> Array;
typedef TArray<long long> Array64;
typedef TArray<double> Arrayf;
typedef TArray<std::pair<int, int>> Arrayp;
typedef TArray<TArray<int>> Array2d;
```
In this document *Array* will be mostly used instead of *TArray&lt;T>*. Usually it means that corresponding method works for arrays of any type; if not, it will be mentioned explicitly.

### Generators
#### template&lt;typename ...Args> <br> static Array Array::random(size_t size, Args... args)
#### template&lt;typename ...Args> <br> static Array Array::randomUnique(size_t size, Args... args)
#### template&lt;typename ...Args> <br> static Array Array::randomAll(Args... args)
* Returns: array of *size* random elements generated as *rnd.tnext&lt;T>(args...)*. In the second version all generated elements are distinct. In the third version generation runs until no new elements appear with high probability.
* Note: *randomUnique* and *randomAll* assume uniform distribution on data. I.e. if your method returns 1 with probability 0.999 and 2 with probability 0.001, *randomUnique(2, ...)* will most likely terminate saying that there are not enough distinct elements.
* Complexity:
    * *random*: *size* calls of *rnd.tnext*;
    * *randomUnique*: approximately *O(size log size)* calls of *rnd.tnext*;
    * *randomAll*: approximately *O(size log size)* calls of *rnd.tnext*, where *size* is the number of generated elements.
* Examples:
```cpp
Array::randomUnique(10, 10)
```
yields a random permutation on 10 elements (though more optimal way is *Array::id(10).shuffled()*);

```cpp
Arrayp::random(20, 10, 10, dpair)
```
yields edges of a random graph with 10 vertices and 20 edges, possibly containing multi-edges, but without loops.

#### template&lt;typename F, typename ...Args> <br> static Array Array::randomf(size_t size, F func, Args... args)
#### template&lt;typename F, typename ...Args> <br> static Array Array::randomfUnique(size_t size, F func, Args... args)
#### template&lt;typename F, typename ...Args> <br> static Array Array::randomfAll(F func, Args... args)
* Same as *Array::random*, but *func(args...)* is called instead of *rnd.tnext*.
* Example:
```cpp
TArray<std::string>::randomf(
    10,
    [](const char* pattern) { return rnd.next(pattern); },
    "[a-z]{5}")
```
yields an array of 10 strings of 5 letters each.

#### Array Array::id(size_t size, T start = T())
* Generates an array of *size* elements: *start*, *start + 1*, ...
* Note: defined only for integer types.

### Modifiers
Most of modifiers have two versions: the one which modifies the object itself and the one which returns the modified copy. They are usually named as *verb* and *verb-ed*, e.g. *shuffle* and *shuffled*.

#### Array& shuffle()
#### Array shuffled() const
* Shuffle the array. The source of randomness is *rnd*.

#### Array& reverse()
#### Array reversed() const
* Reverse the array.

#### Array& sort()
#### Array sorted() const
* Sort the array in non-decreasing order.

####  template&lt;typename Comp> <br> Array& sort(Comp&& comp)
#### template&lt;typename Comp> <br> Array sorted(Comp&& comp) const
* Sort the array in non-decreasing order using *comp* as a comparator.

#### Array& unique()
#### Array uniqued() const
* Remove consequent duplicates in the array. Equivalent to *std::erase(std::unique(a.begin(), a.end()), a.end())*.
* Note: as *std::unique*, this method doesn not remove all duplicated elements if the array is not sorted.

#### Array inverse() const
* Returns: inverse permutation of the array.
* Note: defined only for integer types. Terminates if the array is not a permutation of \[0, n).

#### void extend(size_t requiredSize);
* Equivalent to *resize(max(size(), requiredSize))*.

### Selectors
#### template&lt;typename Integer> <br> Array subseq(const std::vector<Integer>& indices) const;
#### template&lt;typename Integer> <br> Array subseq(const std::initializer_list<Integer>& indices) const;
* Returns: subsequence of the array denoted by *indices*.
* Example:
```cpp
a = a.subseq(Array::id(a.size()).shuffled());
```
effectively shuffles *a*. For example, this may be used to shuffle several arrays with the same permutation.

#### T choice() const;
* Returns: random element of the array.

#### Array choice(size_t count) const;
* Returns: an array of *count* elements of the array **without repetition**.
* Note: obviously, *count* should be not greater than *array.size()*.

#### Array choiceWithRepetition(size_t count) const;
* Returns: an array of *count* elements of the array, possibly repeating.

### Operators
#### Array& operator+=(const Array& other);
#### Array operator+(const Array& other) const;
* Inserts *other* to the end of the array.

#### Array& operator*=(int k);
#### Array operator*(int k) const;
* Repeats the array *k* times.

#### operator std::string() const;
* Casts TArray&lt;char> to std::string.
* Note: defined only for TArray&lt;char>.

--- END DOCUMENT: array.md ---


--- BEGIN DOCUMENT: config.md ---

## Configuration

Jngen has some built-in "sanity checks": if you want to generate an array of size 481927184, likely you have an uninitialize variable. Jngen will gracefully terminate and report it to you (instead of causing OOM error and possibly hanging the machine).

However, sometimes you know better and may want to turn these checks off. To do it, simply put a line at the beginning of *main*:
```cpp
config.optionName = true/false;
```

### List of configurable options (default value)
#### generateLargeObjects (false)
* Allow generating arrays, graphs and so of size exceeding 5 million.

#### largeOptionIndices (false)
* Allow calling *getOpt(n)* for *n >= 32*. This check is created to report if you accidentally call *getOpt('C')* (that is, with char instead of string).

#### normalizeEdges (true)
* If this option is set, edges of newly generated graphs are printed in sorted order to make output more human-readable. You may turn it off if you care about performance rather than presentation.

--- END DOCUMENT: config.md ---


--- BEGIN DOCUMENT: drawer.md ---

## Drawer
Have you ever wanted to visualize tests for geometry problems? Jngen gives you a convenient way to do so. It gives an instrument for drawing
basic geometric primitives (points, circles, segments and polygons) in SVG format.

<img src=pics/img1.png align=left width=28% />
<img src=pics/img2.png align=left width=36% />
<img src=pics/img3.png align=left width=28% />

<br />

Here is a usage example.

```cpp
// Create an instance of a Drawer class
Drawer d;

// Use Point or Pointf from jngen or your own point class.
// In the latter case it must have two fields named x and y.
// Both integers and reals are supported.
Point p1(3, 14);
Point p2(15, 92);

d.point(p1);
// Second argument is radius
d.circle(p1, 5);
d.segment(p1, p2);
// d.polygon takes vector or initializer list of points as its argument
d.polygon(vector<Point>{p1, p2, Point{1, 2}, Point{5, 6}});

// You can also use pairs:
d.point(pair<double, double>(0.5, 1.1));
d.circle(pair<int, int>(5, 6), 10);
d.segment(make_pair(1, 2), make_pair(3, 4));
d.polygon(vector<pair<int, int>>{ {0, 0}, {0, 10}, {10, 0} });

// Or even specify coordinates by hand for point, circle and segment:
d.point(1, 2);
d.circle(5, 10, 3.3);
// Here the order is x1, y1, x2, y2
d.segment(0, 0, 10, 10);

// Style of figures can be altered. Any style change only applies
// to figures which were drawn after.

// You can change the color of your figures...
d.setColor("green");
// and deal with stroke and fill separately:
d.setStroke("red");
d.setFill("blue");
// Both stroke and fill can be set to none passing an empty string:
d.setFill("");
// You can use any color which is supported by HTML/SVG. If the color
// has adequate name it is likely on the list.

// It is possible to set line width (default is 1):
d.setWidth(2.5);
// And opacity (ranging from 0 to 1, 0 is invisible, 1 is solid):
d.setOpacity(0.5);

// By default Jngen draws a cool grid with coordinates. I find it
// very handy, however, if you don't like it it is easy do disable:
d.enableGrid(false);

// Finally, you should save your piece of art to the SVG file:
d.dumpSvg("name.svg");
```

--- END DOCUMENT: drawer.md ---


--- BEGIN DOCUMENT: generic_graph.md ---

## Graphs and trees: common interface

* [Documentation](#document)
* [Weights](#weights)
* [Labeling](#labeling)

Jngen provides a *GenericGraph* class. You will mostly use its two subclasses: *Graph* and *Tree*. They have different generators and methods, though there is a common generic part.

Graph vertices are always numbered from 0 to n-1, where n is the number of vertices. Other numerations will be supported later. Currently can output a graph in 1-numeration using *.add1()* output modifier.

You can assign weights to edges and vertices of a graph. Weight is implemented as (self-written, waiting for C++17) kinda *std::variant* with some predefined types: *int*, *double*, *string*, *pair&lt;int, int>*. However, you can add your own types. To do it define a macro `JNGEN_EXTRA_WEIGHT_TYPES` containing comma-separated extra types you want to use.

```cpp
#define JNGEN_EXTRA_WEIGHT_TYPES std::vector<int>, std::pair<char, double>
#include "jngen.h"
```

Note that if you use precompiled library and compile your code with `JNGEN_DECLARE_ONLY`, you must precompile the library with the same `JNGEN_EXTRA_WEIGHT_TYPES` as well.

Like all containers in jngen, graphs support pretty-printing and output modifiers.

```cpp
Graph g;
g.addEdge(0, 1);
g.addEdge(1, 2);
g.setVertexWeights({"v1", "v2", "v3"});
g.setEdgeWeights({10, 20});

cout << g.printN().printM().add1() << endl;
---
3 2
v1 v2 v3
1 2 10
2 3 20
```

Graphs and trees are printed as following. If *.printN()* and *.printM()* modifiers are set, on the first line *n* and *m* are printed (you can set any of modifiers independently). If vertex weights are present, they are then printed on a separate line. After *m* lines with edges follow. Two endpoints of the edge are printed, optionally followed by edge weight.

**Output modifiers do not apply to vertex/edge weights**. When you set edge length to 10, you probably don't want it to increase to 11 when you switch to 1-numeration, right?

By default, edges of a newly generated graph are printed in sorted order, because it makes tests more human-readable. If you generate large graphs and care about performance rather than presentation, sorting may be disabled using [config](config.md). Simply add this line at the top of *main*:

```cpp
config.normalizeEdges = false;
```

Of course, edges are not sorted anymore after the graph is shuffled.

### Documentation

#### int n() const
* Returns: the number of vertices in the graph.
#### int m() const
* Returns: the number of edges in the graph.
#### bool directed() const
* Returns: true if and only the graph is directed.
#### void addEdge(int u, int v, const Weight& w = Weight{})
* Add an edge *(u, v)*, possbly, with weight *w*, to a graph.
#### bool isConnected() const
* Returns: true if and only if the graph is connected.
#### int vertexByLabel(int label) const
* Returns: the internal id of the vertex identified by *label*. See [*labeling*](#labeling) section at the end of this part. Most likely you'll never need this and the next method.
#### int vertexLabel(int v) const
* Returns: the label of the vertex with internal id *v*.
#### Array edges(int v) const
* Returns: array of vertices incident to *v*.
#### Arrayp edges() const
* Returns: array of all edges of the graph.
#### void setVertexWeights(const WeightArray& weights)
* Set weight of *i*-th vertex to *weights[i]*. Size of *weights* must be equal to *n*.
#### void setVertexWeight(int v, const Weight& weight)
* Set weight of a vertex *v* to *weight*.
#### void setEdgeWeights(const WeightArray& weights)
* Set weight of *i*-th edge to *weights[i]*. Size of *weights* must be equal to *m*.
#### void setEdgeWeight(size_t index, const Weight& weight)
* Set weight of an edge with index *index* to *weight*.
#### Weight vertexWeight(int v) const
* Returns: weight of the vertex *v*.
#### Weight edgeWeight(size_t index) const
* Returns: weight of an edge with index *index*.
#### bool operator==(const GenericGraph& other) const
#### bool operator!=(const GenericGraph& other) const
#### bool operator&lt;(const GenericGraph& other) const
#### bool operator&gt;(const GenericGraph& other) const
#### bool operator&lt;=(const GenericGraph& other) const
#### bool operator&gt;=(const GenericGraph& other) const
* Compare two graphs. If number of vertices in two graphs is different then one with lesser vertices is less than the other. Otherwise adjacency lists of vertices are compared lexicographicaly in natural order of vertices.
* Note: weights have no any effect on comparison result.
* Note: two identical graphs with shuffled adjacency lists are equal.

### Weights
All things you will probably ever do with *Weight* or *WeightArray* are shown in this snippet.

```cpp
Graph g(3); // construct an empty graph on 3 vertices

graph.setVertexWeight(1, 123);
int v = graph.vertexWeight(1); // v = 123
string s = graph.vertexWeight(1); // s = "" because weight holds int now.
cout << graph.vertexWeight(1) << endl; // 123. Value which is now held is printed.
graph.setVertexWeight(2, graph.vertexWeight(1)); // Weight is copyable as wwell.

Array a{1, 2, 3};
graph.setVertexWeights(a); // implicit cast from std::vector<T> to WeightArray
// is supported for each T which can be held by Weight.
std::vector<std::string> vs{"hello", "world", "42"};
graph.setVertexWeights(vs);
```

*Weight* type is implemented as a *jngen::Variant* class. Basically it is a type-safe union which can store the value of any of the predefined types. *jngen::Variant* is a bit different from *boost::variant* and *std::variant*. The first notable exception is that valueless state is valid, i.e. variant can be empty. The second is that *jngen::Variant* allows implicit casts to any of containing types which allows you writing something like

```cpp
int w = graph.vertexWeight(1);
string s = graph.edgeWeight(2);
```

Still, it may have some flaws (I'm far not Antony Polukhin), and I'll be happy to know about them.

### Labeling
Internally graph nodes are stored as integers from 0 to n-1. However, sometimes you need to change numeration (e.g. to shuffle the graph). That's why each vertex is assigned with a *label*, and end-user does all operations with vertices using their labels. Currently labels are always a permutation of [0, n-1]. Later Jngen is going to support arbitrary labeling.

--- END DOCUMENT: generic_graph.md ---


--- BEGIN DOCUMENT: geometry.md ---

## Geometry

Jngen provides two point classes: *Point* with *long long* coordinates and *Pointf* with *long double* coordinates. Standard operations like addition, subtraction, dot and cross products are supported. Similarly, classes *Polygon* and *Polygonf* are provided. A special class *GeometryRandom* is used for generating objects, all interaction goes via its global instance *rndg*.

*Point* is basically a structure with two fields: *x* and *y*. *Polygon* is basically an *Array* of *Points*.

Like most Jngen objects, *Point* and *Polygon* can be printed to streams and modified with [output modifiers](printers.md).

If you are looking for an SVG drawing tool, please refer to [this](drawer.md) page.

### Generators (*rndg* static methods)
#### Point point(long long C)
#### Pointf pointf(long double C)
* Returns: random point with coordinates between 0 and C, inclusive.

#### Point point(long long min, long long max)
#### Pointf pointf(long double min, long double max)
* Returns: random point with coordinates between *min* and *max*, inclusive.

#### Point point(long long x1, long long y1, long long x2, long long y2)
#### Pointf pointf(long double x1, long double y1, long double x2, long double y2)
* Returns: random point with x-coordinate between *x1* and *x2* and y-coordinate between *y1* and *y2*, inclusive.

#### Polygon convexPolygon(int n, long long C)
#### Polygon convexPolygon(int n, long long min, long long max)
#### Polygon convexPolygon(int n, long long x1, long long y1, long long x2, long long y2)
* Returns: random convex polygon with *n* vertices and coordinates lying in specified range.
* No three consecutive vertices lie on the same line, no two points coincide.
* Polygon is generated like following: convex hull of *10n* random points on an ellipse is taken,
    then *n* points are randomly selected from it.
* Throws if the are less than *n* points on the above convex hull.

#### TArray&lt;Point> pointsInGeneralPosition(int n, long long C)
#### TArray&lt;Point> pointsInGeneralPosition(int n, long long min, long long max)
#### TArray&lt;Point> pointsInGeneralPosition(int n, long long x1, long long y1, long long x2, long long y2)
* Returns: *n* random points such that no two coincide and no three lie on the same line.
* Complexity: *O(n<sup>2</sup> log n)*.

### Point and Pointf operators
Here is the list of operators supported for *Point* and *Pointf*. All of them are declared *const*, excluding those which explicitly modify their arguments.

* _p1 + p2_, _p1 += p2_: coordinate-wise addition;
* _p1 - p2_, _p1 -= p2_: coordinate-wise subtraction;
* _p * x_, _p *= x_: coordinate-wise multiplication with scalar value;
* _p1 * p2_: dot product (_p1.x * p2.x + p1.y * p2.y_);
* _p1 % p2_: cross product (_p1.x * p2.y - p1.y * p2.x_);
* _p1 == p2_, _p1 != p2_: coordinate-wise equality comparison;
* _p1 < p2_: lexicographical coordinate-wise ordering.

For *Pointf* comparisons of floating point values are done with *eps* presision. The default value is *10<sup>-9</sup>*. It can be overridden with *setEps* function.

### Polygon and Polygonf methos
*Polygon* inherits *TArray&lt;Point>* so has it supports standard Array methods like *.sort()*, *.choice()* and so on. However, it provides a couple of additional methods.

#### Polygon& shift(const Point& vector)
#### Polygon shifted(const Point& vector) const
* Shift the polygon by given *vector*, i.e. add *vector* to each vertex of a polygon.

#### Polygon& reflect()
#### Polygon reflected() const
* Reflect the polygon across the *x = -y* line, i.e. replace point *(x, y)* with *(-x, -y)*.

--- END DOCUMENT: geometry.md ---


--- BEGIN DOCUMENT: getopt.md ---

## Parsing command-line options
Jngen provides a parser of command-line options. It supports both positional and named arguments. Here is the comprehensive example of usage.

```cpp
// ./main 10 -pi=3.14 20 -hw hello-world randomseedstring
int main(int argc, char *argv[]) {
    parseArgs(argc, argv);
    int n, m;
    double pi;
    string hw;

    n = getOpt(0); // n = 10
    pi = getOpt("pi"); // pi = 3.14

    n = getOpt(5, 100); // n = 100 as there is no option #5
    pi = getOpt("PI", 3.1415); // pi = 3.1415 as there is no option "PI"

    getPositional(n, m); // n = 10, m = 20
    getNamed(hw, pi); // hw = "hello-world", pi = 3.14

    cout << (int)getOpt("none", 10) << endl; // 10 as there is no "none" option
}
```

### Options format
* Any option not starting with "-" sign is a positional option;
* positional options are numbered from 0 sequentially (e.g. if there is a positional option, then named, then again positional, two positional options will have indices 0 and 1);
* named options can have form "-name=value" and "-name value", though the second is allowed if *value* does not start with a hyphen;
* if an option name immediately follows another option name (e.g. "-first -second ..." than the value of *first* is set to 1;
* single "-" sign is ignored;
* anything after "&dash;&dash;" (two minus signs) is ignored;

### Documentation

#### void parseArgs(int argc, char *argv)
* Parse arguments and prepare variable map. Required to be called before any *getOpt...* calls.

#### *unspecified_type* getOpt(size_t index)
#### *unspecified_type* getOpt(const std::string& name)
* Reads an option denoted by *index* (positional, 0-indexed) or *name*. Throws if the option does not exist.
* Return type can be casted to any other type. See the expected usage:
```cpp
int n = getOpt(0), m = getOpt(1);
double h = getOpt("height");
```
* Note: if the cast fails (e.g. you try to interpret "adsfasd" as int) the function throws.

#### template&lt;typename T> <br> *unspecified_type* getOpt(size_t index, T def)
#### template&lt;typename T> <br> *unspecified_type* getOpt(const std::string& name, T def)
* Same as *getOpt(index)* and *getOpt(name)*, but if the option doens't exist then *def* is returned.
* Note: the function still throws if the option exists but the cast fails.

#### bool hasOpt(size_t index)
#### bool hasOpt(const std::string& name)
* Checks if the option denoted by *index* or *name* is present. Its value is not examined.

#### int getPositional(Args&... args)
* Reads positional options to *args...* in order. Arguments which could not be read are not modified.
* Returns: number of succesfully read arguments.

#### int getNamed(Args&... args)
* Reads named arguments. Variable *x* is interpreted as having name *x*. Arguments which could not be read are not modified.
* Returns: number of succesfully read arguments.
* Note: this function is implemented with a define and may be not noticed by your autocompletion tool.

--- END DOCUMENT: getopt.md ---


--- BEGIN DOCUMENT: getting_started.md ---

## Getting started with Jngen

### Installation
Jngen is a single-header library. You only have to download the [jngen.h](https://raw.githubusercontent.com/ifsmirnov/jngen/master/jngen.h)
file and put it somewhere on your machine. `/usr/include` or the directory with your problem must work. And, of course, don't forget to include it
in your source file.

#### Note on compilers
Jngen is known to work with g++ of versions 4.8, 4.9, 5.3 and 6.2 and Clang of version 3.5. You should enable C++11 support (`-std=c++11`)
to work with it. C++14 is also fine.

MS Visual Studio is not supported at the moment, and it is known that Jngen fails to compile under it. Nothing is known about MinGW.

### Migrating from testlib.h
So let's write our first generator for an "A+B" problem!

```cpp
#include "jngen.h"
#include <iostream>
using namespace std;

int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);

    int maxc = getOpt(0);

    int a = rnd.next(0, maxc);
    int b = rnd.next(0, maxc);

    cout << a << " " << b << endl;
}
```

At the first glance there is not much difference from testlib.h. The only new functions are *parseArgs* and *getOpt*.
They are for options parsing. *parseArgs* initializes the parser. *getOpt(0)* reads the first option and casts it to int
(or to any other type, whatever you want). Options parser is described in details [here](getopt.md).

*rnd.next(0, maxc)* returns a random integer from 0 to *maxc*, exactly the same as in testlib.

### The basic Jngen
My favorite and very common example is generating a permutation. I would expect to see something like this:

```cpp
int n = getOpt(0);
vector<int> a;
for (int i = 0; i < n; ++i) {
    a.push_back(i);
}
shuffle(a.begin(), a.end());
cout << n << "\n";
for (int i = 0; i < n; ++i) {
    cout << a[i] + 1;
    if (i+1 == n) {
        cout << "\n";
    } else {
        cout << " ";
    }
}
```

Freaking 14 lines of code! Now see Jngen version.

```cpp
cout << Array::id(getOpt(0)).shuffled().printN().add1() << endl;
```

Such wow, very short. Here we see many Jngen features at once.

* [Arrays](array.md). With *Array::something* you can generate various arrays (like permutations and random ones).
    After you can shuffle, sort and do anything else calling a method on the same object.
* Chaining. Syntax *object.doThis().doThat().andThat()* is very common in Jngen. You will see it when modifying objects
    (like sorting the array), dealing with output format (*printN* and *add1* here) or setting constraints for graphs generation.
* [Printing](printers.md). All containers can be put to *cout* and usually are printed in a least-surprising way. For vector
    and Array it is just space-separated elements. Or newline-separated for 2D; it is smart! With chaining you can print your
    object in 1-numeration and prepend its size to it.

### On the margins
You want [trees](tree.md)? [graphs](graph.md)? [convex polygons](geometry.md)? We have some, but this margin is too narrow to
    contain all of the examples.

```cpp
int h, w;
getPositional(h, w); // also a getOpt-like function
auto a = Tree::bamboo(h);
auto b = Tree::star(w);
cout << a.link(0, b, 0).shuffled() << endl;

cout << Graph::random(n, m).connected().allowMulti().printN().printM() << endl;

Drawer d;
d.polygon(rndg.convexPolygon(n, maxc));
d.dumpSvg("image.svg");
```

I hope that this description and pieces of code helped you to understand how Jngen is supposed to be used.

--- END DOCUMENT: getting_started.md ---


--- BEGIN DOCUMENT: graph.md ---

## Graph generation

* [Generators](#generators)
* [Modifiers](#modifiers)
* [Graph methods](#graph-methods)

This page is about *Graph* class and graph generators. To see the list of generic graphs methods please visit [this page](/generic_graph.md).

The *Graph* class has several static methods to generate random and special graphs, like *random(n, m)* or *complete(n)*. The source of randomness is *rnd*.

After calling a method you can add modifiers to allow or disallow loops, make graph connected etc. As you can see from the following example, *chaining* semantics is used. To support this semantics generation methods return not *Graph* itself but a special proxy class. To get a *Graph* itself, you may do one of the following:
* call *.g()* method after modifiers chain:
* cast the returned object to *Graph*;
* or directly print the proxy class to the stream, in this case the generated graph will be printed.

See the example for further clarifications.

```cpp
auto g = Graph::random(10, 20).connected().allowMulti().g().shuffled();
Graph g2 = Graph::randomStretched(100, 200, 2, 5);
cout << Graph::complete(5).allowLoops() << endl;
```

All graph generators return graph with sorted edges to make tests more human-readable. If you want to have your graph shuffled, use *.shuffle()* method, as in the example.

### Generators
#### random(int n, int m)
* Returns: a random graph with *n* vertices and *m* edges.
* Available modifiers: *connected*, *allowLoops*, *allowMulti*, *directed*, *allowAntiparallel*, *acyclic*.

#### complete(int n)
* Returns: a complete graph with *n* vertices. If *directed* is specified, the direction of each edge is selected randomly, taking into account *allowAntiparallel* and *acyclic* flags.
* Available modifiers: *allowLoops*, *directed*, *allowAntiparallel*, *acyclic*.

#### cycle(int n)
* Returns: a cycle with *n* vertices, connected in order.
* Available modifiers: *directed*.

#### empty(int n)
* Returns: an empty graph with *n* vertices.
* Available modifiers: *directed*.

#### randomStretched(int n, int m, int elongation, int spread)
* Returns: a connected stretched graph with *n* vertices and *m* vertices.
* Available modifiers: *allowLoops*, *allowMulti*, *directed*, *allowAntiparallel*, *acyclic*.
* Description: first a random tree on *n* vertices with given *elongation* (see [tree docs](/doc/tree.md)) is generated. Then remaining *m*-*n*+*1* edges are added. One endpoint of an edge is selected at random. The second is a result of jumping to a tree parent of the first endoint a random number of times, from 0 to *spread*, inclusive.
* If the graph is directed, the direction of each edge is selected at random, unless it is acyclic: in this case the direction of all edges is down the tree.

#### randomBipartite(int n1, int n2, int m)
* Returns: a random bipartite graph with *n1* vertices in one part, *n2* vertices in another part and *m* edges. Vertices from *1* to *n1* belong to the first part.
* Available modifiers: *connected*, *allowMulti*.

#### completeBipartite(int n1, int n2)
* Returns: a complet bipartite graph with *n1* vertices in one part and *n2* vertices in another part. Vertices from *1* to *n1* belong to the first part.
* Available modifiers: none.

### Modifiers
All options are unset by default. If the generator contradicts some option (like *randomStretched*, which always produces a connected graph), it is ignored.
#### connected(bool value = true)
* Action: force the generated graph to be connected.
#### allowMulti(bool value = true)
* Action: allow multiple edges in the generated graph (i.e. several edges with the same endpoints).
#### allowLoops(bool value = true)
* Action: allow loops in the generated graph (i.e. edges from a vertex to itself).
#### directed(bool value = true)
* Action: create a directed graph.
#### allowAntiparallel(bool value = true)
* Action: allow antiparallel edges (that is, edges u-v and v-u) in a directed graph. Ignored if *directed* is unset.
#### acyclic(bool value = true)
* Action: make the directed graph acyclic (DAG). Ignored if *directed* is unset.

### Graph methods
#### Graph(int n)
* Construct an empty graph with *n* vertices.
#### void setN(int n)
* Set the number of vertices of the graph to *n*.
* Note: this operation cannot lessen the number of vertices.

#### Graph& shuffle()
#### Graph shuffled() const
* Shuffle the graph. This means:
    * relabel vertices in random order;
    * shuffle edges;
    * randomly swap egdes' endpoints (for undirected graphs only).

#### Graph& shuffleAllBut(const Array& except)
#### Graph shuffledAllBut(const Array& except)
* Same as *shuffle*, but vertices from *except* do not change their numbers.
    * Possible usecase: we may generate a graph where *s-t* path is supposed to be found. Then shuffle the graph in such a way that path endpoints are still *1* and *n*:
```cpp
g = Graph::random(n, m)...;
g.shuffleAllBut({0, n-1});
```

--- END DOCUMENT: graph.md ---


--- BEGIN DOCUMENT: library_build.md ---

## Accelerating Jngen build

Jngen is distributed as a single header. As the header is sufficiently large, compilation lasts fairly long. To speed it up you may use `JNGEN_DECLARE_ONLY` macro.

Many functions in the library look like this:

```cpp
#ifdef JNGEN_DECLARE_ONLY
void doSomething();
#else
void doSomething() {
    // crunching numbers
}
#endif
```

If `JNGEN_DECLARE_ONLY` is defined, the compiler expects to find the definitions in some other translation unit, otherwise the header is used standalone. When working with Jngen locally, you may create a static library which includes *jngen.h* and does nothing else, compile it with *g++ lib.cpp -c*, and then link your *main.cpp* with generated *lib.o*. If you add `#define JNGEN_DECLARE_ONLY` to the top of your *main.cpp* or specify `-DJNGEN_DECLARE_ONLY` flag in compiler options, function definitions will be taken from the static library and thus will be not recompiled every time.

```sh
$ echo '#include "jngen.h"' > lib.cpp
$ g++ -O2 -std=c++11 -Wall lib.cpp -c
$ g++ -O2 -std=c++11 -Wall -DJNGEN_DECLARE_ONLY main.cpp lib.o
```

On the author's laptop this trick reduces compilation time by approximately 2.5 times.

Note that if you use some other Jngen defines, like `JNGEN_EXTRA_WEIGHT_TYPES`, the library and your program must be compiled with the same set of defines.


--- END DOCUMENT: library_build.md ---


--- BEGIN DOCUMENT: math.md ---

## Math-ish primitives

Jngen provides several free functions and a generator class *MathRandom* to help generating numbers and combinatorial primitives. All interaction with *MathRandom* goes via its global instance called *rndm*. The source of randomness is *rnd*.

### Standalone functions

#### bool isPrime(long long n)
* Returns: true if *n* is prime, false otherwise.
* Supported for all *n* from 1 to 3.8e18.
* Implemented with deterministic variation of the Miller-Rabin primality test so should work relatively fast (exact benchmark here).

### MathRandom methods

#### long long randomPrime(long long n)
#### long long randomPrime(long long l, long long r)
* Returns: random prime in range *[2, n)* or *[l, r]* respectively.
* Throws if no prime is found on the interval.

#### long long nextPrime(long long n)
#### long long previousPrime(long long n)
* Returns: the first prime larger (or smaller) than *n*, including *n*.

#### Array partition(int n, int numParts, int minSize = 0, int maxSize = -1)
* Returns: a random ordered partition of *n* into *numParts* parts, where the size of each part is between *minSize* and *maxSize*. If *maxSize* is *-1* (the default value) then sizes can be arbitrary large.

#### template&lt;typename T> <br> TArray&lt;TArray&lt;T>> partition(TArray&lt;T> elements, int numParts, int minSize = 0, int maxSize = -1)
* Returns: a random partition of the array *elements* into *numParts* parts.

#### template&lt;typename T> <br> TArray&lt;TArray&lt;T>> partition(TArray&lt;T> elements, const Array& sizes)
* Returns: a random partition of the array *elements* into parts, where the size of each part is specified.
* Note: sum(*sizes*) must be equal to *elements.size()*.

--- END DOCUMENT: math.md ---


--- BEGIN DOCUMENT: overview.md ---

## Overview

Jngen is a library which helps you to generate standard objects for competitive problems: trees, graphs, strings and so. For some objects it defines classes (like *Array*, *Graph* or *Point*), for others STL is used (*std::string*).

<!-- Primitive generators are provided (like «generate a random tree»), as well as testsets which contain various tests which you would likely use in your problem anyway. -->

There are two ways of generating objects. The first is with static methods of the class.

```cpp
auto a = Array::random(n, maxSize);
auto t = Tree::bamboo(n);
```

[Arrays](array.md), [trees](tree.md) and [graphs](graph.md) are generated like this.

The second uses helper objects.


```cpp
auto polygon = rndg.convexPolygon(n, maxCoordinate);
auto stringPair = rnds.antiHash({{1000000007, 101}, {1000000009, 211}}, "a-z", 10000);
int p = rndm.randomPrime(100, int(1e9));
```

[Strings](strings.md), [geometric primitives](geometry.md), [primes and partitions](math.md) and simply [random numbers](random.md) are generated with such helpers.

For each Jngen object there are operators for printing to streams. There are modifiers which allow, for example, to switch between 0- and 1-indexation. Also Jngen allows printing standard containers like vectors and pairs. See section [printers](printers.md).

```cpp
cout << std::vector<int>{1, 2, 3} << endl;
cout << Array::id(5).shuffled().printN().add1() << endl;
---
1 2 3
5
5 2 4 3 1
```

The library also supplies a [command-line arguments parser](getopt.md) and a [tool for drawing geometric primitives](drawer.md).

Jngen is large, its compilation lasts for several seconds. It is possible to make it faster with precompiling a part of it. See [this chapter](library_build.md) for manual.

If you want to learn more about Jngen, please see all the docs listed at the [reference](/README.md#reference) section. Good luck!

--- END DOCUMENT: overview.md ---


--- BEGIN DOCUMENT: printers.md ---

## Printing to ostreams

Tired of writing `cout << a[i] << " \n"[i+1 == n]`? We have a solution! Jngen declares ostream operators for all standard containers. Moreover, for Jngen containers there is a bunch of output modifiers which can toggle 0/1 numeration, automatically print the size of the array and something else.

### Outline
As a quick start, try to write something like
```cpp
cout << Array::random(5, 5) << endl;
cout << Arrayp::random(2, 10) << endl;
---
3 1 1 0 4
5 9
8 8
```

Or even
```cpp
vector<int> a{0, 1, 2};
pair<string, double> p{"hello", 4.2};
cout << a << endl;
cout << p << endl;
---
0 1 2
hello 4.2
```
Containers are printed in a least surprising way: sequences are separated with single spaces, sequences of pairs -- with line breaks, sequences of sequences are formatted as matrices. If you print a graph, it first prints *n* and *m* on the first line (if corresponding modifiers are set, see later), then, if present, a line of vertex weights, then *m* lines with edges in a most standard format.

Now a word about modifiers. C++ programmers are used to 0-indexing, while in problem statements usually arises 1-indexing. There is a *quick fix*, which at first glance looks as a dirty hack but later appears to be very convenient. Look how to output a random 1-indexed permutation:
```cpp
cout << Array::id(5).shuffled().add1().printN() << endl;
---
5
1 4 2 5 3
```
These *add1()* and *printN()* are called *output modifiers*. These modifiers can be applied to any container provided by Jngen, such as Array, Graph and Tree. If you want to use modifiers with other types (like std::vector or even int), you can do it like this:
```cpp
vector<int> a{1, 2, 3};
cout << repr(a).endl() << endl;
---
1
2
3
```

### Global modifier
Sometimes it may be more convenient to set modifiers once for the entire program. This can be done as following:
```cpp
setMod().printN().add1();
// now printN() and add1() modifiers apply to everything being printed
setMod().reset();
// global modifier has returned to default state, you should specify local modifiers manually
```

Note that Jngen does not interact with stl-defined operators. That mean that writing `cout << 123 << endl;` will print *123* regardless of which global modifiers are set. However, printing a std::vector **will** use global modifiers.

### Modifiers
#### add1(bool value = true)
* Action: adds 1 to each integer being output, **except for vertex/edge weights in graphs**.
* Default: unset.
#### printN(bool value = true)
* Action: print array size on a separate line before the array. Print number of vertices of a graph.
* Default: unset.
#### printM(bool value = true)
* Action: print number of edges of a graph.
* Default: unset.
#### printEdges(bool value = true)
* Action: when printing a tree, print a list of edges.
* Default: set.
#### printParents(int value = -1)
* Action: when printing a tree, print a parent of each vertex. Opposite to *printEdges*.
* Arguments: *value* stands for the root of the tree. If *value* is *0* or greater, then the parent of each vertex is printed, having root's parent as
    *-1* (*0* if *add1()* is present). *value = -1* is a special value: in this case tree is rooted at *0* and its parent is not printed (printing *n-1* values in total).
* Note: this option and *printEdges* cancel each other.
#### endl(bool value = true)
* Action: separate elements of the array with line breaks instead of spaces.
* Default: unset.

--- END DOCUMENT: printers.md ---


--- BEGIN DOCUMENT: random.md ---

## Random numbers generation

Jngen provides a class *Random* whose behavior is similar to *rnd* from testlib.h. E.g. you may write *rnd.next(100)*, *rnd.next("[a-z]{%d}", n)*, and so on.  Most of interaction with *Random* happens via its global instance of *Random* called *rnd*.

Default initialized *Random* is seeded with some hardware-generated random value, so subsequent executions of the program will produce different tests. This may be useful for local stress-testing, for example. If you want to fix the seed, use *registerGen(argc, argv)* at the beginning of your *main*.

### Generation

#### uint32_t next()
* Returns: random integer in range [0, 2^32).
#### uint64_t next64()
* Returns: random integer in range [0, 2^64).
#### double nextf()
* Returns: random real in range [0, 1).
#### int next(int n) // also for long long, size\_t, double
* Returns: random integer in range [0, n).
#### int next(int l, int r) // also for long long, size\_t, double
* Returns: random integer in range [l, r].
#### int wnext(int n, int w) // also for long long, size\_t, double
* If w > 0, returns max(next(n), ..., next(n)) (w times). If w &lt; 0, returns min(next(n), ..., next(n)) (-w times). If w = 0, same as next(n).
#### int wnext(int l, int r, int w) // also for long long, size\_t, double
* Same as wnext(n, w), but the range is [l, r].
#### std::string next(const std::string& pattern)
* Should be compatible with testlib.h.
* Returns: random string matching regex *pattern*.
* Regex has the following features:
    * any single character yields itself;
    * a set of characters inside square braces (*[abc123]*) yields random of them;
    * character ranges are allowed inside square braces (*[a-z1-9]*);
    * pattern followed by *{n}* is the same as the pattern repeated *n* times;
    * pattern followed by *{l,r}* is the same as the pattern repeated random number of times from *l* to *r*, inclusive;
    * "|" character yields either a pattern to its left or the pattern to its right equiprobably;
    * several "|" characters between patterns yield any pattern between them equiprobably, e.g. *(a|b|c|z){100}* yields a string of length 100 with almost equal number of *a*'s, *b*'s, *c*'s and *z*'s;
    * parentheses "()" are used for grouping.
* examples:
    * `rnd.next("[1-9][0-9]{1,2}")`:  random 2- or 3-digit number (note that the distribution on numbers is not uniform);
    * `rnd.next("a{10}{10}{10}")`: 1000 *a*'s;
    * `rnd.next("(ab|ba){10}|c{15}")`: either 15 *c*'s or a string of length 20 consisting of *ab*'s and *ba*'s.
#### std::string next(const std::string& pattern, ...)
* Same as rnd.next(pattern), but pattern interpreted as printf-like format string.
#### template&lt;typename T, typename ...Args> <br> tnext(Args... args)
* Calls *next(args...)*, forcing the return type to be *T* and casting arguments appropriately. E.g. *tnext&lt;int>(2.5, 10.1)* is equivalent to *rnd.next(2, 10)*, where both arguments are ints.
* Name origin: *typed* next.
#### std::pair&lt;int, int> nextp(int n, [RandomPairTraits])
#### std::pair&lt;int, int> nextp(int l, int r, [RandomPairTraits])
* Returns: random pair of integers, where both of them are in range [0, *n*) or [*l*, *r*] respectively.
* RandomPairTraits denotes if the pair should be ordered (first element is less than or equal to second one) and if its two elements should be distinct. Several global constants are defined:
    * *opair*: ordered pair (first &lt;= second)
    * *dpair*: distinct pair (first != second)
    * *odpair*, *dopair*: ordered distinct pair
* Example of usage:  *rnd.nextp(1, 10, odpair)* yields a pair of random integers from 1 to 10 where first is strictly less than second. *rnd.nextp(1, 10)* returns any pair of integers from 1 to 10 (note that the *RandomPairTraits* argument is optional).
#### template&lt;typename Iterator> <br> Iterator::value_type choice(Iterator begin, Iterator end)
#### template&lt;typename Container> <br> Container::value_type choice(const Container& container)
* Returns: random element of a range or of a container, respectively.
* Note: *Container* may be *any* STL container, including *std::set*. In general case the runtime of this function is *O(container.size())*. However, if *Iterator* is a random-access iterator, the runtime is constant.

#### template&lt;typename N> <br> size_t nextByDistribution(const std::vector&lt;N>& distribution)
* Returns: a random integer from *0* to *distribution.size() - 1*, where probability of *i* is proportional to *distribution[i].
* Example: *rnd.nextByDistribution({1, 1, 100})* will likely return 2, but roughly each 50-th iteration will return 0 or 1.

### Seeding
#### void seed(uint32_t seed)
#### void seed(const std::vector&lt;uint32_t>& seed)
* Seed the generator with appropriate values. It is guaranteed that after identical *seed* calls the generator produces the same sequence of values.

### Related free functions
#### void registerGen(int argc, char* argv[], [int version])
* Seed the generator using command-line options. Different options will likely result in different generator states. The behavior is similar to the one of testlib.h.
* Note: parameter *version* is optional and is introduced only for compatibility with testlib.h.

--- END DOCUMENT: random.md ---


--- BEGIN DOCUMENT: strings.md ---

## Strings

Strings are generated with the help of *StringRandom* class. As usual, you should interact with it via its global instance *rnds*.

### Generators (*rnds* static methods)
#### std::string random(int len, const std::string& alphabet = "a-z")
* Returns: random string of length *len* made of characters from *alphabet*.
* Note: *alphabet* can contain single chars and groups of form *A-Z*. For example, *"0-9abcdefA-F"* includes all hexadecimal characters.

#### std::string random(const std::string& pattern, ...)
* Returns: a random string generated by *pattern*.
* Equivalent to *rnd.next(pattern, ...)*; see [docs on Random](random.md) for detailed description.

#### std::string thueMorse(int len, char first = 'a', char second = 'b')
* Returns: a prefix of length *n* of the Thue-Morse string made of *first* and *second* characters.
* Description: Thue-Morse string is a string of kind 0110100110010110.... That is, start from 0 and on each step concatenate the string to itself exchanging zeroes and ones.
* Note: this string is useful for breaking hashes modulo 2<sup>64</sup>. Strings *thueMorse(n, x, y)* and *thueMorse(n, y, x)* will have identical polynomial hash for any base for *n* &ge; 2048.

#### std::string abacaba(int len, char first = 'a')
* Returns: a prefix of length *n* of the string of form *abacabadabacaba...* starting with character *first*.

#### std::pair&lt;std::string, std::string> antiHash(<br>&emsp;&emsp;const std::vector&lt;std::pair&lt;long long, long long>>& bases, <br>&emsp;&emsp;const std::string& alphabet = "a-z", <br>&emsp;&emsp;int length = -1)
* Returns: a pair of different strings of length *length* (or minimal found if *length* is -1) with the same polynomial hash for specified bases.
* Parameters:
    * *bases*: vector of pairs (mod, base);
    * *alphabet*: the same as in *random(len, alphabet)*;
    * *length*: length of resulting strings, or *-1* if the shortest found result is needed.
* Note: mod must not exceed 2\*10<sup>9</sup>. Also, you cannot specify more than two pairs (mod, base).
* Complexity and result size: for two mods around 2\*10<sup>9</sup> generation runs for about 3 seconds and produces strings of length approximately 100-200. A faster version of the algorithm will be presented later.
* Example:
```cpp
int mod1 = rndm.randomPrime(1999000000, 2000000000);
int mod2 = rndm.randomPrime(1999000000, 2000000000);
int base1 = rnd.next(2000, 10000) * 2 + 1;
int base2 = rnd.next(2000, 10000) * 2 + 1;

auto res = rnds.antiHash( {{mod1, base1}, {mod2, base2}}, "a-z", -1);
cout << res.first << "\n" << res.second << "\n";

// or simply
cout << rnds.antiHash({{1000000007, 107}, {1000000009, 109}}) << "\n";
```

--- END DOCUMENT: strings.md ---


--- BEGIN DOCUMENT: tree.md ---

## Trees generation

Jngen provides a *Tree* class. It offers some methods to manipulate with trees and static generators. As other Jngen objects, *Tree* can be printed to *std::ostream*. Here is a standard way to use generators:

```cpp
cout << Tree::random(100).shuffled() << endl;
```

### Generators
Note that all generators return trees with sorted edges to make tests more human-readable. More, numbering is not always random for same reason. Particularly, *Tree::random(size, elongation)* always returns a tree rooted at 0. You can always use *tree.shuffle()*  to renumerate vertices and shuffle edges.

#### random(int size)
* Returns: a completely random tree, selected uniformly over all n<sup>n-2</sup> trees. Name comes from the fact that this generator exploits Prüfer sequences.

#### randomPrim(int size, int elongation = 0)
* Returns: a random tree with given elongation built with Prim-like process. The most classical tree generator ever.
* Description: first, vertex no. 0 is selected as a root. Next, for each vertex from 1 to n-1 its parent is selected as *wnext(i, elongation)*. With *elongation = -1000000* you will likely get a star, with *elongation = 1000000* -- a bamboo (a path).

#### randomKruskal(int size)
* Returns: a random tree built with a Kruskal-like process.
* Description: uniformly random edges are added one by one. The edge is added if it doesn't introduce a cycle.

#### bamboo(int size)
* Returns: a bamboo (or a path) of a kind 0 -- 1 -- ... -- n-1.

#### star(int size)
* Returns: a star graph with *size* vertices and vertex no. 0 in the center. Central vertex is counted, i.e. there are *size - 1* leaf vertices in general case.

#### caterpillar(int size, int length)
* Returns: a caterpillar tree with *size* vertices based on a path of length *length*.
* Description: first, a path of length *length* is generated. Vertices of the path are numbered in order. Next, other *size - length* vertices are connected to random vertices of the path.

#### Tree binary(int size)
* Returns: a complete binary tree with *size* vertices.
* Numeration: parent of vertex *i* is *(i-1)/2*, *0* is root.

#### Tree kary(int size, int k)
* Returns: a complete *k*-ary tree with *size* vertices.
* Numeration: parent of vertex *i* is *(i-1)/k*, *0* is root.

#### Tree fromPruferSequence(const Array& code)
* Returns: a tree with given [Prüfer sequence](https://en.wikipedia.org/wiki/Pr%C3%BCfer_sequence). The tree contains *code.size() + 2* vertices.

### Tree methods

#### Tree& shuffle()
#### Tree shuffled() const
* Shuffle the tree. This means:
    * relabel vertices in random order;
    * shuffle edges;
    * randomly swap egdes' endpoints.

#### Tree& shuffleAllBut(const Array& except)
#### Tree shuffledAllBut(const Array& except)
* Same as *shuffle*, but vertices from *except* do not change their numbers.
    * Possible usecase: we may generate a rooted tree and shuffle it in such a way that root still has number *1*.
```cpp
t = Tree::randomPrim(n, 1000);
t.shuffleAllBut({0});
```

#### Array parents(int root) const
* Returns: array of size *n*, where *i*-th element is a parent of vertex *i* if the tree is rooted at *root*. Parent of *root* is *-1*.

#### Tree link(int vInThis, const Tree& other, int vInOther)
* Returns: a tree made of _*this_ and *other*, with an extra edge between two vertices with ids *vInThis* and *vInOther*, respectively.
* Labeling: labels of the source tree are unchanged, labels of the other tree are increased by the number of vertices in source. Edges are ordered like "source edges, other edges, new edge".

#### Tree glue(int vInThis, const Tree& other, int vInOther)
* Returns: a tree made of _*this_ and *other*, where vertices *vInThis* and *vInOther* are glued into one.
* Labeling: labels of the source tree are unchanged, vertices of the other tree are renumbered in order starting with the number of vertices in source, except for *vInOther*.

--- END DOCUMENT: tree.md ---


--- BEGIN EXAMPLE: 786D.cpp ---

#include "jngen.h"
using namespace std;

// http://codeforces.com/contest/786/problem/D
// tree with a letter on each edge, then pairs of distinct vertices
// run as ./main n, m [-elong=...]
int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);

    int n = getOpt(0);
    int q = getOpt(1);
    int elong = getOpt("elong", 0);

    cout << n << " " << q << "\n";
    auto t = Tree::randomPrim(n, elong).shuffled();
    t.setEdgeWeights(TArray<char>::random(n - 1, 'a', 'z'));
    cout << t.add1() << "\n";
    cout << Arrayp::random(q, 1, n, dpair) << "\n";
}

--- END EXAMPLE: 786D.cpp ---


--- BEGIN EXAMPLE: even-odd.cpp ---

#include "jngen.h"
#include <bits/stdc++.h>
using namespace std;
#define forn(i, n) for (int i = 0; i < (int)(n); ++i)

#define se second
#define fi first

Graph connectedBipartite(int n, int m) {
    Tree t = Tree::random(n);
    vector<int> q{0};
    vector<int> col(n, -1);
    col[0] = 0;
    Array bc[2];
    bc[0] = {0};
    forn(i, n) {
        int v = q[i];
        for (int to: t.edges(v)) {
            if (col[to] == -1) {
                col[to] = !col[v];
                bc[col[to]].push_back(to);
                q.push_back(to);
            }
        }
    }
    m = min<long long>((long long)m, 1ll * bc[0].size() * bc[1].size());
    auto treeEdges = t.edges();
    Graph g(t);
    set<pair<int, int>> edges(treeEdges.begin(), treeEdges.end());
    while ((int)edges.size() != m) {
        int u = bc[0].choice();
        int v = bc[1].choice();
        if (!edges.count({v, u}) && edges.emplace(u, v).second) {
            g.addEdge(u, v);
        }
    }
    return g.shuffled();
}

Graph makeTreeOfGraphs(const std::vector<Graph>& graphs, bool line = false) {
    Array shifts;
    int s = 0;
    int n = graphs.size();
    forn(i, n) {
        shifts.push_back(s);
        s += graphs[i].n();
    }

    jngen::Dsu dsu;
    dsu.getRoot(s - 1);

    auto t = line ? Tree::bamboo(n) : Tree::random(n);
    for (auto e: t.edges()) {
        int v1 = rnd.next(shifts[e.fi], shifts[e.fi] + graphs[e.fi].n() - 1);
        int v2 = rnd.next(shifts[e.se], shifts[e.se] + graphs[e.se].n() - 1);
        dsu.unite(v1, v2);
    }

    map<int, int> id;
    forn(i, s) {
        int v = dsu.getRoot(i);
        if (!id.count(v)) {
            int t = id.size();
            id[v] = t;
        }
    }

    Graph res(id.size());
    set<pair<int, int>> edges;
    forn(i, n) for (auto e: graphs[i].edges()) {
        int v1 = e.first + shifts[i];
        int v2 = e.second + shifts[i];
        v1 = id[dsu.getRoot(v1)];
        v2 = id[dsu.getRoot(v2)];
        if (v1 != v2 && !edges.count({v1, v2}) && !edges.count({v2, v2})) {
            edges.emplace(v1, v2);
            res.addEdge(v1, v2);
        }
    }
    return res;
}

int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);

    setMod().printN().printM().add1();

    string type = getOpt("type", "random");

    if (type == "random") {
        int n, m;
        ensure(getPositional(n, m) == 2);
        ensure(n >= 2);
        cout << Graph::random(n, m).connected().g().shuffled() << endl;
    } else if (type == "bipartite") {
        int n, m;
        ensure(getPositional(n, m) == 2);
        cout << connectedBipartite(n, m) << endl;
    } else if (type == "bipartite-tree") {
        int n, m;
        ensure(getPositional(n, m) == 2);
        int n_comps = getOpt("n_comps", 5);
        int n_bad = getOpt("n_bad", 0);
        Array vnums = rndm.partition(n, n_comps, /* min_size = */ 1);
        Array enums = vnums;
        for (int& x: enums) {
            --x;
            m -= x;
        }
        auto ePartition = rndm.partition(m, n_comps, /* min_size = */ 1);
        forn(i, n_comps) enums[i] += ePartition[i];
        TArray<Graph> parts;
        forn(i, n_comps) {
            if (rnd.next(n_comps - i) < n_bad) {
                --n_bad;
                parts.push_back(Graph::random(
                    vnums[i], min<long long>(enums[i], 1ll * vnums[i] * (vnums[i] - 1) / 2)).connected()
                    );
            } else {
                parts.push_back(connectedBipartite(vnums[i], enums[i]));
            }
        }
        auto g = makeTreeOfGraphs(parts);
//         cout << Array::id(g.n()).endl().printN(false)  << endl;
//         cout << g.printN(false).printM(false) << endl;
        cout << g.shuffled() << endl;
    } else if (type == "manual") {
        int n = getOpt(0);
        int id = getOpt("id");
        if (id == 1) {
            const int k = 100;
            vector<Graph> graphs;
            forn(i, k) {
                graphs.push_back(connectedBipartite(n / (k*2), n / k));
            }
            auto g = makeTreeOfGraphs(graphs, true);
            cout << g.shuffled() << endl;
        } else if (id == 2) {
            const int k = 100;
            vector<Graph> graphs;
            forn(i, k) {
                if (i%2 == 0) {
                    graphs.push_back(connectedBipartite(n / (k*2), n / k));
                } else {
                    graphs.push_back(Graph::complete(3));
                }
            }
            auto g = makeTreeOfGraphs(graphs, true);
            cout << g.shuffled() << endl;
        } else if (id == 3) {
            cout << Graph(Tree::bamboo(n)).shuffled() << endl;
        } else if (id == 4) {
            cout << Graph(Tree::star(n)).shuffled() << endl;
        } else {
            ensure(false, format("Incorrect manual test id: '%d'", id));
        }
    } else {
        ensure(false, format("Type '%s' is not supported", type.c_str()));
    }

    return 0;
}

--- END EXAMPLE: even-odd.cpp ---


--- BEGIN EXAMPLE: folding.cpp ---

#include "jngen.h"
#include <bits/stdc++.h>
#define forn(i, n) for (int i = 0; i < (int)(n); ++i)
using namespace std;

Tree uniDepthTree(const vector<int>& layers) {
    ensure(is_sorted(layers.begin(), layers.end()));

    Tree t;
    Array last{0};
    int n = 1;
    for (int d: layers) {
        Array nxt = Array::id(d, n);
        n += d;
        Array cnt(last.size(), 1);
        forn(i, d - last.size()) ++cnt[rnd.next() % cnt.size()];
        int ptr = 0;
        forn(i, cnt.size()) {
            forn(j, cnt[i]) {
                t.addEdge(last[i], nxt[ptr++]);
            }
        }
        last = nxt;
    }
    return t;
}

Array depthVector(int n, int depth) {
    ensure(n >= depth);

    Array a(depth, 1);
    n -= depth;

    while (n) {
        int k = rnd.next(1, min(depth, n));
        forn(i, k) {
            ++a[depth - i - 1];
        }
        n -= k;
    }
    return a;
}

Tree goodTree(int n, int a, int b) {
    int deg = rnd.next(1, int(sqrt(n)));

    Array sz(deg, 1);
    forn(i, n - deg - 1) ++sz[rnd.next(sz.size())];

    Tree t;
    for (int x: sz) {
        int d;
        if (min(a, b) > x) {
            continue;
        } else if (max(a, b) > x) {
            d = min(a, b);
        } else {
            d = rnd.next(0, 1) ? a : b;
        }

        auto u = uniDepthTree(depthVector(x, d));
        t = t.glue(0, u, 0);
    }

    return t;
}

Tree distort(Tree t, int cnt) {
    int n = t.n();
    forn(i, cnt) {
        t.addEdge(rnd.next(n), n);
        ++n;
    }
    return t.shuffle();
}

void genSpecial(int id) {
    if (id == 1) {
        cout << distort(Tree::bamboo(180001), 50).shuffled() << endl;
    } else if (id == 2) {
        cout << Tree::star(200000).shuffled() << endl;
    } else if (id == 3) {
        cout << distort(Tree::star(190000), 1000).shuffled() << endl;
    } else if (id == 4 || id == 5) {
        Tree a = Tree::bamboo(98000);
        Tree b = Tree::star(98000);
        a = a.link(0, b, 0);

        if (id == 5) {
            a = distort(a, 200);
        }

        cout << a.shuffled() << endl;
    } else if (id == 6) {
        cout << Tree::caterpillar(200000, 50000).shuffled() << endl;
    } else if (id == 7) {
        cout << Tree::caterpillar(20000, 150000).shuffled() << endl;
    }
}

int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);

    setMod().printN().add1();

    string type;
    int n, a = -1, b = -1;

    getPositional(type, n, a, b);

    if (a == -1) {
        cerr << "a = -1" << endl;
        a = rnd.next(1, int(sqrt(n)));
    }
    if (b == -1) {
        cerr << "b = -1" << endl;
        b = rnd.next(1, int(sqrt(n)));
    }

    if (type == "yes") {
        cout << goodTree(n, a, b).shuffled() << endl;
    }

    if (type == "no") {
        int bad = rnd.next(1, min(n, 10));
        cout << distort(goodTree(n - bad, a, b).shuffled(), bad) << endl;
    }

    if (type == "bamboo") {
        cout << Tree::bamboo(n).shuffled() << endl;
    }

    if (type == "special") {
        genSpecial(n);
    }
}


--- END EXAMPLE: folding.cpp ---


--- BEGIN EXAMPLE: jumps.cpp ---

#include "jngen.h"
#include <bits/stdc++.h>
using namespace std;
#define forn(i, n) for (int i = 0; i < (int)(n); ++i)

int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);
    setMod().printN();

    int n;
    ensure(getOpt(0, n));

    string type = getOpt("type", "random");

    if (type == "random") {
        int min = 1, max = n-1;
        getNamed(min, max);

        auto a = Array::random(n, min, max);

        cout << a << "\n";
    } else if (type == "manual") {
        int id;
        ensure(getNamed(id));

        if (id == 1) {
            Array a(n, 1);
            a[0] = a[n-1] = n-1;
            cout << a << "\n";
        } else if (id == 2) {
            cout << Array(n, 1) << "\n";
        } else if (id == 3) {
            cout << Array(n, n-1) << "\n";
        } else if (id == 4) {
            cout << Array{1, 2} * (n/2) << "\n";
        } else {
            ensure(false, format("Incorrect manual test id: '%d'", id));
        }
    } else if (type == "sides") {
        int sidelen = 0, smin = 1, smax = n-1, min = 1, max = n-1;
        getNamed(sidelen, smin, smax, min, max);
        ensure(2 * sidelen <= n);

        auto lhs = Array::random(rnd.wnext(1, sidelen, 3), smin, smax);
        auto rhs = Array::random(rnd.wnext(1, sidelen, 3), smin, smax);
        auto mid = Array::random(n - lhs.size() - rhs.size(), min, max);

        cout << lhs + mid + rhs << "\n";
    } else if (type == "islands") {
        int cnt = 1, size = n, min = 1, max = n-1;
        getNamed(cnt, size, min, max);
        ensure(cnt * (size + 1) - 1 <= n);
        auto landSizes = rndm.partition(n - cnt*size, cnt+1, /* minSize = */ 1);
        Array a;
        forn(i, cnt) {
            a += Array(landSizes[i], n-1);
            a += Array::random(size, min, max);
        }
        a += Array(landSizes.back(), n-1);
        cout << a << "\n";
    } else {
        ensure(false, format("Incorrect type: '%s'", type.c_str()));
    }

    return 0;
}

--- END EXAMPLE: jumps.cpp ---


--- BEGIN EXAMPLE: some_random_graph_problem.cpp ---

#include "jngen.h"
#include <bits/stdc++.h>
using namespace std;
#define forn(i, n) for (int i = 0; i < (int)(n); ++i)
#define for2(cur, prev, a) for (auto _it1 = std::begin(a),\
        _it2 = _it1 == std::end(a) ? _it1 : std::next(_it1);\
        _it2 != std::end(a); ++_it1, ++_it2)\
        for (bool _ = true; _;)\
        for (auto &cur = *_it1, &prev = *_it2; _; _ = false)

Array getw(int m) {
    int minc = 0, maxc = 9;
    getNamed(minc, maxc);
    return Array::random(m, minc, maxc);
}

int main(int argc, char *argv[]) {
    registerGen(argc, argv);
    parseArgs(argc, argv);

    setMod().printN().printM().add1();

    if (int id = getOpt("manual", 0)) {
        int n = getOpt(0, -1);
        int m = getOpt(1, -1);
        (void)(n+m);

        if (id == 1) {
            cout << "2 1\n1 2 5\n";
        } else if (id == 2) {
            cout << "2 1\n1 2 0\n";
        } else if (id == 3) {
            Graph g = Tree::bamboo(n);
            g.setEdgeWeights(Array::random(n-1, 0, 9));
            g.shuffleAllBut({0, n-1});
            cout << g << endl;
        } else if (id == 4) {
            Graph g = Tree::bamboo(n);
            g.setEdgeWeights(Array::random(n-1, 0, 0));
            g.shuffleAllBut({0, n-1});
            cout << g << endl;
        } else if (id == 5) {
            Graph g = Tree::bamboo(n);
            g.setEdgeWeights(Array::random(n-1, 1, 9));
            g.shuffleAllBut({0, n-1});
            cout << g << endl;
        } else if (id == 6) {
            Graph g = Tree::star(n);
            g.setEdgeWeights(Array::random(n-1, 1, 9));
            g.shuffle();
            cout << g << endl;
        } else if (id == 7) {
            Graph g(n);
            forn(i, n-1) {
                g.addEdge(i, i+1);
                g.setEdgeWeight(g.m()-1, rnd.next(0, 9));
                g.addEdge(i, i+1);
                g.setEdgeWeight(g.m()-1, rnd.next(0, 9));
            }
            g.shuffleAllBut({0, n-1});
            cout << g << endl;
        } else {
            ensure(false, format("manual test id unknown: %d", id));
        }

        return 0;
    }


    int n = getOpt(0);
    int m = getOpt(1);

    string type = getOpt("type", "random");

    if (type == "random") {
        auto g = Graph::random(n, m).connected().allowMulti(true).g();
        g.setEdgeWeights(getw(m));
        g.shuffle();
        cout << g << endl;
    } else if (type == "stretched") {
        int elong = getOpt("elong", 10);
        int spread = getOpt("spread", 5);

        auto g = Graph::randomStretched(n, m, elong, spread).
            connected().allowMulti(true).g();
        g.setEdgeWeights(getw(m));
        g.shuffleAllBut({0, n-1});

        cout << g << endl;
    } else if (type == "levels") {
        int mn = getOpt("min", 1);
        int mx = getOpt("max", 10);
        auto levels = rndm.partition(Array::id(n-2, 1), (n-2) / ((mn + mx)/2), mn, mx);
        levels.insert(levels.begin(), {0});
        levels.push_back({n-1});

        Graph g;

        for2(prev, cur, levels) {
            for (auto v: cur) {
                g.addEdge(v, prev.choice());
                --m;
            }
        }
        while (m) {
            int l1 = rnd.next(1u, levels.size() - 1);
            int v = levels[l1-1].choice();
            int to = levels[l1].choice();
            g.addEdge(v, to);
            --m;
        }
        g.setEdgeWeights(getw(g.m()));

        cout << g << endl;
    } else {
        ensure(false, "Unknown test type");
    }

    return 0;
}

--- END EXAMPLE: some_random_graph_problem.cpp ---
