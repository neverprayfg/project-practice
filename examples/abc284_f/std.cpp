#include<bits/stdc++.h>
using namespace std;
using ll = long long;
int n;
string t;

ll binpow(ll a , ll b , ll mod) {
    ll r = 1;
    while(b) {
        if(b % 2) r = (r * a)%mod;
        b /= 2;
        a = (a * a)%mod;
    }
    return r;
}

int main() {
    cin >> n >> t;
    int m = t.length();
    string r = t;
    reverse(r.begin() , r.end());
    
    vector<ll> p(m + 1);
    p[0] = 1;
    ll mod = 1e9 + 9;
    for(int i = 0 ; i < m ; i++){
        p[i+ 1] = (1LL*p[i]*31)%mod;    
    }
    
    vector<ll>ht(m + 1);
    vector<ll>hr(m + 1);
    
    for(int i = 0 ; i < m ; i++){
        ht[i + 1] = (ht[i] + ((t[i] - 'a' + 1)*p[i])%mod)%mod;
        hr[i + 1] = (hr[i] + ((r[i] - 'a' + 1)*p[i])%mod)%mod;
    }
    
    for (int i = 0; i <= n; i++) {

    // prefix = T[0..i-1]
    ll ph = ht[i];

    // suffix = T[n+i .. 2n-1]
    ll sh = (ht[m] - ht[n+i] + mod) % mod;
    sh = sh * binpow(p[n+i], mod-2, mod) % mod;

    // hash(prefix + suffix)
    ll hs = (ph + sh * p[i]) % mod;

    // reverse(middle)
    // middle = T[i .. i+n-1]
    // reverse(middle) = R[n-i .. 2n-1-i]
    ll md = (hr[2*n-i] - hr[n-i] + mod) % mod;
    md = md * binpow(p[n-i], mod-2, mod) % mod;

    if (hs == md) {
        string s = t.substr(0, i) + t.substr(n+i);
        cout << s << '\n';
        cout << i << '\n';
        return 0;
    }
}

cout << -1 << '\n';
    
    return 0;
}