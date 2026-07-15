#include <bits/stdc++.h>
using namespace std;

#define ll long long
#define rep(i, n) for(int i = 0; i < n; ++i)
#define N (int)8
#define MOD (ll)998244353

int n,k;
int d[N][N]; //whether A_j exist in the left side of i-th re-arranged constaint
ll s[N]; //S'_j

vector<int>a[1<<8]; //3k(max.24)bit
vector<ll>c[1<<8];  //count

void pre_calc(void){
	unordered_map<int,int>tmp[1<<8];
	rep(bits,1<<n){
		int x[8]={};
		int y=0,z=0;
		rep(i,k){
			rep(j,n){
				if(d[i][j]==1)x[i]+=(bits>>j)&1;
			}
		}
		rep(j,k){
			y|=(x[j]&1)<<j; //k bit
			z|=(x[j]>>1)<<(j*3); //3k bit
		}
		tmp[y][z]=tmp[y][z]+1;
	}
	rep(i,1<<k){
		unordered_map<int,int>::iterator itr=tmp[i].begin();
		while(itr!=tmp[i].end()){
			a[i].push_back((*itr).first);
			c[i].push_back((*itr).second);
			itr++;
		}
	}
	return;
}




int main(void){
	int tmpl,tmpr;
	ll tmps;
	int m;
	int base[N+1];
	ll offset[N+1];

	//receive input
	cin>>n>>m;

	rep(i,n+1){
		base[i]=i;
		offset[i]=0;
	}

	rep(i,m){
		cin>>tmpl>>tmpr>>tmps;
		tmpl--;
		tmps-=(tmpr-tmpl); //Ai: positive -> non-negative
		if(tmps<0){
			cout<<"0"<<endl;
			return 0;
		}
		if(base[tmpl]==base[tmpr]){
			if(offset[tmpr]-offset[tmpl]!=tmps){
				cout<<"0"<<endl;
				return 0;
			}
		}
		int overwrite_before, overwrite_after;
		ll add_offset;
		if(base[tmpl]<base[tmpr]){
			overwrite_before=base[tmpr];
			overwrite_after=base[tmpl];
			add_offset=offset[tmpl]-offset[tmpr]+tmps;
		}
		else{
			overwrite_before=base[tmpl];
			overwrite_after=base[tmpr];
			add_offset=offset[tmpr]-offset[tmpl]-tmps;
		}
		rep(j,n+1){
			if(base[j]==overwrite_before){
				base[j]=overwrite_after;
				offset[j]+=add_offset;
			}
		}
	}

	//set re-arranged constraints
	int x[30][8];  //x[i][j] i-th bit of S'_j
	bool used[8]={}; //check for the existence of un-constrained Ai
	k=0; //M'
	rep(i,N)rep(j,N)d[i][j]=0;
	rep(i,n){
		for(int j=i+1;j<=n;j++){
			if(base[i]==base[j]){
				if(offset[j]-offset[i]<0){
					cout<<"0"<<endl;
					return 0;
				}
				for(int ii=i;ii<j;ii++){
					d[k][ii]=1;
					used[ii]=true;
				}
				s[k]=offset[j]-offset[i];
				for(int ii=0;ii<30;ii++)x[ii][k]=(s[k]>>ii)&1;
				k++;
				break;
			}
		}
	}

	pre_calc();

	vector<int>dpkey={0}; //3k bit
	vector<ll>dpcnt={1};
	rep(i,30){
		int sz=dpkey.size();
		unordered_map<int,ll>mp;
		rep(j,sz){
			int y[8]={};
			rep(ii,k)y[ii]=(dpkey[j]>>(3*ii))&1;
			int pre=0; //3k bit
			rep(ii,k)pre|=(dpkey[j]>>1)&(3<<(3*ii));
			int add_key=0;//k bit
			rep(ii,k){
				add_key|=(x[i][ii]^y[ii])<<ii;
				if((x[i][ii]==0)&&(y[ii]==1))pre+=1<<(3*ii);
			}
			int sz2=a[add_key].size();
			rep(ii,sz2){
				mp[a[add_key][ii]+pre]=mp[a[add_key][ii]+pre]+(c[add_key][ii]*dpcnt[j]);
			}
		}
		dpkey.clear();
		dpcnt.clear();
		unordered_map<int,ll>::iterator itr=mp.begin();
		while(itr!=mp.end()){
			dpkey.push_back((*itr).first);
			dpcnt.push_back((*itr).second%MOD);
			itr++;
		}
	}

	//judge zero/non-zero and get remainder
	bool found=false;
	ll ans=0;
	int sz=dpkey.size();
	rep(i,sz){
		if(dpkey[i]==0){
			found=true;
			ans=dpcnt[i];
		}
	}
	if(found){
		rep(i,n){
			if(!used[i]){
				cout<<"Infinity"<<endl;
				return 0;
			}
		}
		cout<<ans<<endl;
	}
	else{
		cout<<0<<endl;
	}
	
	return 0;
}
